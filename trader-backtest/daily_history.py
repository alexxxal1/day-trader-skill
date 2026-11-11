#!/usr/bin/env python3
"""Pure daily-history study. No intraday, entry, broker or execution dependency.

NumPy/Pandas accelerate only data preparation. Signal decisions call the
unchanged native Minervini evaluator and Qullamaggie breakout function.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import csv
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import statistics
import time

import numpy as np
import pandas as pd

from common import digest, module, read, stamp, write
from daily_collect import validate_response

NAMES = ("MINERVINI", "BREAKOUT_CANDIDATE", "BREAKOUT_SETUP_READY", "CONFLUENCE_CANDIDATE", "CONFLUENCE_SETUP_READY")
BITS = (1, 2, 4, 8, 16)
HORIZONS = (1, 3, 5, 10, 20, 60)


def mapped(path, shape, dtype, mode="r"):
    return np.memmap(str(path), mode=mode, dtype=dtype, shape=tuple(shape))


class HistoricalClock:
    """Verified UTC schedule, including pre-2007 New York DST rules."""
    def __init__(self, calendar):
        self.schedule = calendar["sessions"]
        self.days = sorted(self.schedule)
        self.mini = module("trader-scanner", "minervini")

    def to_eastern(self, value):
        return pd.Timestamp(value).tz_localize("UTC").tz_convert("America/New_York").tz_localize(None).to_pydatetime()

    def eastern_to_utc(self, value):
        return pd.Timestamp(value).tz_localize("America/New_York").tz_convert("UTC").tz_localize(None).to_pydatetime()

    def ts_to_eastern(self, value):
        return pd.Timestamp(value, unit="s", tz="UTC").tz_convert("America/New_York").tz_localize(None).to_pydatetime()

    def session(self, day):
        row = self.schedule.get(day.isoformat())
        return tuple(self.to_eastern(stamp(row[k])).time() for k in ("open_utc", "close_utc")) if row else None

    def close(self, day):
        return stamp(self.schedule[str(day)]["close_utc"])

    def minervini_module(self):
        return self.mini


def continuous_runs(valid):
    index = np.arange(len(valid))
    last_invalid = np.maximum.accumulate(np.where(valid, -1, index))
    prior = index - last_invalid
    next_invalid = np.minimum.accumulate(np.where(valid, len(valid), index)[::-1])[::-1]
    after = np.r_[next_invalid[1:] - index[:-1] - 1, 0]
    return prior, np.minimum(60, after).astype("uint8")


def normalize(result, days):
    """No hole compression or current quote metadata substitution."""
    day_index = {d: i for i, d in enumerate(days)}
    local_days = pd.to_datetime(result["timestamp"], unit="s", utc=True).tz_convert("America/New_York").strftime("%Y-%m-%d")
    positions = np.array([day_index.get(d, -1) for d in local_days], dtype=int)
    q = result["indicators"]["quote"][0]
    values = {}
    for key, name in (("c", "close"), ("h", "high"), ("l", "low"), ("v", "volume"), ("o", "open")):
        out = np.full(len(days), np.nan)
        column = np.asarray(q.get(name, []), dtype=float)
        take = (positions >= 0) & (np.arange(len(positions)) < len(column))
        selected = positions[take]
        if len(selected) != len(np.unique(selected)):
            raise ValueError("DUPLICATE_DAILY_SESSION")
        out[selected] = column[np.flatnonzero(take)]
        values[key] = out
    valid = np.isfinite(values["c"]) & np.isfinite(values["h"]) & np.isfinite(values["l"]) & np.isfinite(values["v"])
    valid &= (values["l"] > 0) & (values["l"] <= values["c"]) & (values["c"] <= values["h"]) & (values["v"] >= 0)
    for key in values:
        values[key][~valid] = np.nan
    factor = np.ones(len(days))
    for split in (result.get("events") or {}).get("splits", {}).values():
        day = pd.Timestamp(split["date"], unit="s", tz="UTC").tz_convert("America/New_York").strftime("%Y-%m-%d")
        ratio = float(split["numerator"]) / float(split["denominator"])
        if not np.isfinite(ratio) or ratio <= 0:
            raise ValueError("INVALID_SPLIT_EVENT")
        factor[np.asarray(days) < day] *= ratio
    values["factor"] = factor
    return values, valid


def prepare(data, output):
    data, output = Path(data), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    cache = output / "stock-cache"
    cache.mkdir(exist_ok=True)
    frozen = read(data / "frozen-acquisition.json")
    manifest = read(data / "acquisition.json")
    calendar = read(data / "calendar.json")
    days = sorted(calendar["sessions"])
    universe = frozen["universe"]
    shape = (len(days), len(universe))
    stock_shape = (shape[1], shape[0])
    c = mapped(output / "close.f64", stock_shape, "float64", "w+")
    c[:] = np.nan
    e = mapped(output / "eligible.u8", stock_shape, "uint8", "w+")
    e[:] = 0
    after = mapped(output / "forward-contiguous.u8", stock_shape, "uint8", "w+")
    after[:] = 0
    ranks = mapped(output / "ranks.f64", (4,) + stock_shape, "float64", "w+")
    ranks[:] = np.nan
    records = {r["ticker"]: r for r in manifest["records"]}
    coverage = []
    interval_stream = (output / "daily-eligibility-intervals.csv").open("w", newline="")
    intervals = csv.writer(interval_stream)
    intervals.writerow(["ticker", "from_session", "through_session", "status"])
    for i, sym in enumerate(universe):
        record = records[sym]
        audit = dict(record)
        if record["status"] != "loaded":
            audit["daily_exclusion"] = "DAILY_FETCH_FAILED"
            intervals.writerow([sym, days[0], days[-1], "DAILY_FETCH_FAILED"])
            coverage.append(audit)
            continue
        body = gzip.decompress((data / record["raw_file"]).read_bytes())
        if hashlib.sha256(body).hexdigest() != record["raw_sha256"]:
            raise ValueError("raw data checksum changed: " + sym)
        try:
            result = validate_response(body, sym)
            if result["meta"].get("currency") != "USD":
                raise ValueError("NON_USD_QUOTES")
            if result["meta"].get("instrumentType") != "EQUITY":
                raise ValueError("NON_EQUITY_QUOTES")
            values, valid = normalize(result, days)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            audit["daily_exclusion"] = str(exc)
            intervals.writerow([sym, days[0], days[-1], str(exc)])
            coverage.append(audit)
            continue
        prior, following = continuous_runs(valid)
        eligible = prior >= 260
        c[i, :] = values["c"]
        e[i, :] = eligible
        after[i, :] = following
        for k, period in enumerate((21, 63, 126, 252)):
            with np.errstate(divide="ignore", invalid="ignore"):
                f = values["factor"][period:]
                returns = np.r_[np.full(period, np.nan), 100 * ((values["c"][period:] * f) / (values["c"][:-period] * f) - 1)]
            ranks[k, i, :] = np.where(eligible, returns, np.nan)
        np.savez_compressed(cache / (sym + ".npz"), **values)
        valid_indices = np.flatnonzero(valid)
        eligible_indices = np.flatnonzero(eligible)
        audit.update(valid_sessions=int(valid.sum()), common_eligible_sessions=int(eligible.sum()),
            first_valid=days[valid_indices[0]] if len(valid_indices) else None,
            last_valid=days[valid_indices[-1]] if len(valid_indices) else None,
            first_eligible=days[eligible_indices[0]] if len(eligible_indices) else None,
            last_eligible=days[eligible_indices[-1]] if len(eligible_indices) else None,
            daily_exclusion=None if len(eligible_indices) else "NO_COMPLETE_260_SESSION_WINDOW")
        reasons = np.full(len(days), "ELIGIBLE", dtype=object)
        reasons[~eligible] = "WARMUP_OR_GAP_IN_260_SESSION_WINDOW"
        reasons[~valid] = "MISSING_OR_INVALID_DAILY_BAR"
        if len(valid_indices):
            reasons[:valid_indices[0]] = "NO_PROVIDER_HISTORY_YET"
            reasons[valid_indices[-1] + 1:] = "STALE_OR_TRUNCATED_PROVIDER_HISTORY"
        changes = np.r_[0, np.flatnonzero(reasons[1:] != reasons[:-1]) + 1, len(days)]
        for a, b in zip(changes[:-1], changes[1:]):
            intervals.writerow([sym, days[a], days[b - 1], reasons[a]])
        coverage.append(audit)
        if (i + 1) % 250 == 0:
            print("Daily preparation", i + 1, "/", len(universe), flush=True)
    interval_stream.close()
    for array in (c, e, after, ranks):
        array.flush()
    del c, e, after, ranks
    # Sequential writes by ticker above, then a blocked transpose. Direct
    # column writes repeatedly dirty almost every file page for each ticker.
    for filename, dtype, layers in (("close.f64", "float64", 1), ("eligible.u8", "uint8", 1),
                                     ("forward-contiguous.u8", "uint8", 1), ("ranks.f64", "float64", 4)):
        source_shape = stock_shape if layers == 1 else (layers,) + stock_shape
        target_shape = shape if layers == 1 else (layers,) + shape
        source = mapped(output / filename, source_shape, dtype)
        target_path = output / (filename + ".transposed")
        target = mapped(target_path, target_shape, dtype, "w+")
        for begin in range(0, shape[0], 128):
            end = min(begin + 128, shape[0])
            if layers == 1:
                target[begin:end, :] = source[:, begin:end].T
            else:
                target[:, begin:end, :] = source[:, :, begin:end].transpose(0, 2, 1)
        target.flush()
        del source, target
        os.replace(str(target_path), str(output / filename))
    e = mapped(output / "eligible.u8", shape, "uint8")
    ranks = mapped(output / "ranks.f64", (4,) + shape, "float64", "r+")
    # Rank before signal selection, over exactly the same contemporaneous
    # 260-session data population for every strategy. Original rank function.
    mini = module("trader-scanner", "minervini")
    for day in range(len(days)):
        eligible = np.flatnonzero(e[day])
        for k in range(4):
            population = {str(i): float(ranks[k, day, i]) for i in eligible}
            ranked = mini.percentile_ranks(population)
            ranks[k, day, :] = np.nan
            for i in eligible:
                if str(i) in ranked:
                    ranks[k, day, i] = ranked[str(i)]
    ranks.flush()
    benchmarks = {}
    for sym in ("SPY", "QQQ"):
        record = records[sym]
        if record["status"] != "loaded":
            raise ValueError("benchmark history missing: " + sym)
        result = validate_response(gzip.decompress((data / record["raw_file"]).read_bytes()), sym)
        values, valid = normalize(result, days)
        benchmarks[sym] = {"close": values["c"].tolist(), "valid": valid.tolist()}
    available = np.asarray(benchmarks["SPY"]["valid"]) & (e.sum(axis=1) >= 2)
    positions = np.flatnonzero(available)
    if not len(positions):
        raise ValueError("no common SPY/strategy period")
    meta = {"shape": shape, "days": days, "universe": universe,
        "test_first_index": int(positions[0]), "test_last_index": len(days) - 1,
        "test_start": days[positions[0]], "test_end": days[-1], "common_history": 260,
        "calendar_sha256": digest(calendar), "acquisition_sha256": digest(manifest),
        "frozen_sha256": digest(frozen), "flags": manifest["flags"] + ["PRICE_RETURNS_EXCLUDE_DIVIDENDS", "NO_HISTORICAL_SECURITY_IDENTIFIER_ARCHIVE"]}
    # JSON stays strict; missing prices are null, never NaN literals.
    for row in benchmarks.values():
        row["close"] = [v if np.isfinite(v) else None for v in row["close"]]
    write(output / "benchmarks.json", benchmarks)
    write(output / "ticker-coverage.json", coverage)
    write(output / "matrix-metadata.json", meta)
    print("Common daily test period", meta["test_start"], "to", meta["test_end"], flush=True)
    return meta


def native_decisions(c, h, l, volume, eligible, rank, days, trend_days=21, factor=None):
    """Feature adapter + original decision functions, no fitted parameters."""
    mini = module("trader-scanner", "minervini")
    qull = module("trader-scanner", "qullamaggie")
    cf = module("trader-confluence", "confluence")
    factor = np.ones(len(c)) if factor is None else factor
    series = pd.Series(c)
    metrics = {
        "sma50": series.rolling(50).mean().to_numpy(),
        "sma150": series.rolling(150).mean().to_numpy(),
        "sma200": series.rolling(200).mean().to_numpy(),
        "sma200_previous": series.shift(trend_days).rolling(200).mean().to_numpy(),
        "low_52w": pd.Series(l).rolling(252).min().to_numpy(),
        "high_52w": pd.Series(h).rolling(252).max().to_numpy(),
    }
    flags = np.zeros(len(c), dtype="uint8")
    counts = {"eligible": int(eligible.sum()), "minervini_native_calls": 0, "breakout_native_calls": 0,
              "minervini_pass": 0, "breakout_failed": 0, "breakout_candidate": 0, "breakout_setup_ready": 0}
    for d in np.flatnonzero(eligible & (rank[3] >= 70)):
        values = {name: float(values[d]) * float(factor[d]) for name, values in metrics.items()}
        values["price"] = float(c[d]) * float(factor[d])
        # Near strict boundaries, use the native sequential sums on the exact
        # historical price basis. Rolling acceleration cannot flip an equality.
        tests = [(values[a], values[b]) for a, b in (
            ("price", "sma50"), ("price", "sma150"), ("price", "sma200"),
            ("sma50", "sma150"), ("sma50", "sma200"), ("sma150", "sma200"),
            ("sma200", "sma200_previous"))]
        if any(abs(a - b) <= 1e-10 * max(1, abs(a), abs(b)) for a, b in tests):
            for name, n, lag in (("sma50", 50, 0), ("sma150", 150, 0), ("sma200", 200, 0), ("sma200_previous", 200, trend_days)):
                values[name] = sum(float(x) * float(factor[d]) for x in c[d - lag - n + 1:d - lag + 1]) / n
        result = mini.evaluate("ASSET", values, float(rank[3, d]))
        counts["minervini_native_calls"] += 1
        if result["trend_template"] == "PASS":
            flags[d] |= 1
    cfg = qull.config()
    # No leader -> native breakout can never be CANDIDATE/SETUP_READY.
    leaders = eligible & np.any(rank[:3] >= cfg["leader_percentile"], axis=0)
    rows = [dict(date=dt.date.fromisoformat(day), close=float(c[i]), high=float(h[i]), low=float(l[i]),
                 volume=float(volume[i])) for i, day in enumerate(days)]
    for d in np.flatnonzero(leaders):
        ranks = {p: float(rank[k, d]) for k, p in enumerate(("1M", "3M", "6M"))}
        sample = rows[d - 126:d + 1]
        f = float(factor[d])
        if f != 1:
            sample = [dict(r, close=r["close"] * f, high=r["high"] * f, low=r["low"] * f, volume=r["volume"] / f) for r in sample]
        status, _, _, _ = qull.breakout(sample, ranks, cfg)
        counts["breakout_native_calls"] += 1
        counts["breakout_" + status.lower()] += 1
        if status == "CANDIDATE":
            flags[d] |= 2
        elif status == "SETUP_READY":
            flags[d] |= 4
        if status in ("CANDIDATE", "SETUP_READY"):
            _, level = cf.classify("PASS" if flags[d] & 1 else "FAIL", {"setup": "BREAKOUT", "status": status})
            if level:
                flags[d] |= 8 if status == "CANDIDATE" else 16
    counts["minervini_pass"] = int(np.count_nonzero(flags & 1))
    return flags, counts


def _stock_decisions(job):
    output, i = job
    output = Path(output)
    meta = read(output / "matrix-metadata.json")
    sym = meta["universe"][i]
    if not (output / "stock-cache" / (sym + ".npz")).exists():
        return i, None, {}
    shape = meta["shape"]
    eligible = mapped(output / "eligible.u8", shape, "uint8")[:, i].astype(bool)
    eligible[:meta["test_first_index"]] = False
    ranks = mapped(output / "ranks.f64", (4,) + tuple(shape), "float64")[:, :, i]
    with np.load(output / "stock-cache" / (sym + ".npz")) as cache:
        flags, counts = native_decisions(cache["c"], cache["h"], cache["l"], cache["v"], eligible, ranks, meta["days"], factor=cache["factor"])
    return i, flags, counts


def replay_daily(output, workers=2):
    output = Path(output)
    meta = read(output / "matrix-metadata.json")
    signals = mapped(output / "signals.u8", meta["shape"], "uint8", "w+")
    signals[:] = 0
    details = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for n, (i, flags, counts) in enumerate(pool.map(_stock_decisions, [(str(output), i) for i in range(len(meta["universe"]))], chunksize=8), 1):
            if flags is not None:
                signals[:, i] = flags
            details.append(dict(ticker=meta["universe"][i], **counts))
            if n % 250 == 0:
                signals.flush()
                print("Native daily strategy replay", n, "/", len(meta["universe"]), flush=True)
    signals.flush()
    write(output / "native-call-audit.json", details)
    return meta


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("prepare", "replay", "all"))
    p.add_argument("--data", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--workers", type=int, default=2)
    a = p.parse_args()
    if a.command in ("prepare", "all"):
        prepare(a.data, a.output)
    if a.command in ("replay", "all"):
        replay_daily(a.output, a.workers)
