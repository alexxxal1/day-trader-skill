#!/usr/bin/env python3
"""Uniform retrospective source-quality checks. Never repairs or deletes prices."""
import argparse
import csv
import gzip
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from common import digest, read, write
from daily_history import mapped

VALID, SUSPECT, INVALID = 0, 1, 2
REASONS = {
    1: "INVALID_NUMERIC_OR_OHLCV", 2: "DAILY_CLOSE_FACTOR_GE2_OR_LE_HALF",
    4: "ZERO_VOLUME_CHANGED_CLOSE", 8: "STALE_ZERO_VOLUME_20_SESSIONS",
    16: "UNRECONCILED_SPLIT_UNITS", 32: "CONFIRMED_MIXED_QUOTE_ADJCLOSE_UNITS",
    64: "ADJUSTMENT_RATIO_CHANGE_WITHOUT_ACTION", 128: "HISTORY_BEFORE_DECLARED_INCEPTION",
    256: "SOURCE_MARKET_OR_IDENTITY_MISMATCH", 512: "INVALID_SOURCE_INSTRUMENT_OR_CURRENCY",
    1024: "INVALID_ADJCLOSE_DIAGNOSTIC", 2048: "MISSING_DAILY_SESSION",
}


def window_quality(bar, feature, day, horizon, last):
    if day + horizon > last:
        return np.full(bar.shape[1], INVALID, dtype="uint8")
    return np.maximum(feature[day], np.max(bar[day + 1:day + horizon + 1], axis=0))


def classify(result, days, config):
    """Returns one severity and reason bitset per exchange date."""
    n = len(days)
    day_map = {d: i for i, d in enumerate(days)}
    t = result.get("timestamp") or []
    local = pd.to_datetime(t, unit="s", utc=True).tz_convert("America/New_York").strftime("%Y-%m-%d")
    positions = np.asarray([day_map.get(d, -1) for d in local], dtype=int)
    raw = result["indicators"]["quote"][0]
    prices = {}
    present = np.zeros(n, dtype=bool)
    present[positions[positions >= 0]] = True
    for name in ("open", "high", "low", "close", "volume"):
        column = np.asarray(raw.get(name) or [], dtype=float)
        target = np.full(n, np.nan)
        take = (positions >= 0) & (np.arange(len(positions)) < len(column))
        target[positions[take]] = column[np.flatnonzero(take)]
        prices[name] = target
    status = np.zeros(n, dtype="uint8")
    reason = np.zeros(n, dtype="uint32")

    def flag(mask, severity, bit):
        status[mask] = np.maximum(status[mask], severity)
        reason[mask] |= bit

    c, h, l, o, v = (prices[k] for k in ("close", "high", "low", "open", "volume"))
    numeric = np.isfinite(c) & np.isfinite(h) & np.isfinite(l) & np.isfinite(o) & np.isfinite(v)
    numeric &= (np.minimum.reduce([c, h, l, o]) > 0) & (v >= 0) & (l <= np.minimum(o, c)) & (np.maximum(o, c) <= h)
    flag(~numeric & present, INVALID, 1)
    flag(~present, INVALID, 2048)
    with np.errstate(divide="ignore", invalid="ignore"):
        gross = c / np.r_[np.nan, c[:-1]]
    jump = np.isfinite(gross) & ((gross >= config["daily_gross_jump_factor"]) | (gross <= 1 / config["daily_gross_jump_factor"]))
    flag(jump, SUSPECT, 2)
    changed = np.isfinite(gross) & (gross != 1)
    flag((v == 0) & changed, SUSPECT, 4)
    stale = np.isfinite(c) & (v == 0) & np.isfinite(gross) & (gross == 1)
    count = np.arange(n) - np.maximum.accumulate(np.where(stale, -1, np.arange(n)))
    flag(count >= config["stale_zero_volume_sessions"], SUSPECT, 8)
    actions = []
    action_dates, dividend_dates = set(), set()
    splits = []
    for kind in ("splits", "dividends", "capitalGains"):
        for event in (result.get("events") or {}).get(kind, {}).values():
            date = pd.Timestamp(event["date"], unit="s", tz="UTC").tz_convert("America/New_York").strftime("%Y-%m-%d")
            d = day_map.get(date)
            if d is None:
                continue
            action_dates.update(range(max(0, d - 1), min(n, d + 2)))
            if kind == "dividends":
                dividend_dates.update(range(max(0, d - 1), min(n, d + 2)))
            row = dict(event, action=kind, session=date, session_index=d)
            if kind == "splits":
                ratio = float(event["numerator"]) / float(event["denominator"])
                row["ratio"] = ratio
                row["action_type"] = "REVERSE_SPLIT" if ratio < 1 else "SPLIT"
                row["quote_gross_at_action"] = float(gross[d]) if np.isfinite(gross[d]) else None
                splits.append((d, ratio))
            actions.append(row)
    adj = np.full(n, np.nan)
    supplied_adj = bool(result["indicators"].get("adjclose"))
    if supplied_adj:
        values = np.asarray(result["indicators"]["adjclose"][0].get("adjclose") or [], dtype=float)
        take = (positions >= 0) & (np.arange(len(positions)) < len(values))
        adj[positions[take]] = values[np.flatnonzero(take)]
        flag(present & (~np.isfinite(adj) | (adj <= 0)), SUSPECT, 1024)
    with np.errstate(divide="ignore", invalid="ignore"):
        adj_gross = adj / np.r_[np.nan, adj[:-1]]
        adjustment = adj / c
        adjustment_change = adjustment / np.r_[np.nan, adjustment[:-1]]
    allowance = config["adjustment_ratio_discontinuity_factor"]
    changes = np.isfinite(adjustment_change) & ((adjustment_change > allowance) | (adjustment_change < 1 / allowance))
    no_action = np.ones(n, dtype=bool)
    for d in action_dates:
        no_action[d] = False
    flag(changes & no_action, SUSPECT, 64)
    tolerance = config["split_match_ratio_tolerance"]
    for d, ratio in splits:
        if not np.isfinite(ratio) or ratio <= 0:
            flag(np.arange(n) == d, INVALID, 1)
            continue
        for k in range(max(1, d - 1), min(n, d + 2)):
            resembles_raw = jump[k] and 1 / tolerance <= gross[k] * ratio <= tolerance
            if resembles_raw:
                mask = np.arange(n) == k
                flag(mask, SUSPECT, 16)
                corrected = np.isfinite(adj_gross[k]) and .5 < adj_gross[k] < 2
                mixed_units = corrected and 1 / tolerance <= (adj_gross[k] / gross[k]) / ratio <= tolerance
                if mixed_units:
                    flag(mask, INVALID, 32)
    meta = result.get("meta") or {}
    first_trade = meta.get("firstTradeDate")
    if first_trade:
        first_date = pd.Timestamp(first_trade, unit="s", tz="UTC").tz_convert("America/New_York").strftime("%Y-%m-%d")
        first_pos = int(np.searchsorted(days, first_date))
        flag(present & (np.arange(n) < first_pos - config["pre_first_trade_grace_sessions"]), SUSPECT, 128)
    if meta.get("exchangeTimezoneName") not in (None, "America/New_York", "US/Eastern"):
        flag(present, SUSPECT, 256)
    if meta.get("currency") != "USD" or meta.get("instrumentType") != "EQUITY":
        flag(present, INVALID, 512)
    return status, reason, present, actions


def build(data, analysis, output):
    data, analysis, output = Path(data), Path(analysis), Path(output)
    meta = read(analysis / "matrix-metadata.json")
    method = read(output / "frozen-methodology.json")
    if method != read(Path(__file__).parent / "event-methodology.json"):
        raise ValueError("quality rules changed after freeze")
    config = method["quality"]
    d, n = meta["shape"]
    stock_shape = (n, d)
    files = (("quality-bars.u8", "uint8"), ("quality-features.u8", "uint8"), ("quality-reasons.u32", "uint32"))
    bar = mapped(output / files[0][0], stock_shape, "uint8", "w+")
    features = mapped(output / files[1][0], stock_shape, "uint8", "w+")
    reasons = mapped(output / files[2][0], stock_shape, "uint32", "w+")
    bar[:], features[:], reasons[:] = INVALID, INVALID, 2048
    records = {r["ticker"]: r for r in read(data / "acquisition.json")["records"]}
    summaries = []
    with gzip.open(output / "quality-issues.csv.gz", "wt", newline="") as issues, (output / "corporate-actions.csv").open("w", newline="") as corporate:
        wi, wc = csv.writer(issues), csv.writer(corporate)
        wi.writerow(["ticker", "session", "label", "reason_bits", "reasons"])
        wc.writerow(["ticker", "session", "action", "ratio_or_amount", "action_type", "quote_gross_at_action"])
        for i, sym in enumerate(meta["universe"]):
            record = records[sym]
            if record["status"] != "loaded":
                summaries.append({"ticker": sym, "source_status": record["status"], "identity_archive": "UNKNOWN_NOT_AVAILABLE"})
                continue
            body = gzip.decompress((data / record["raw_file"]).read_bytes())
            doc = json.loads(body)["chart"]["result"][0]
            s, r, present, actions = classify(doc, meta["days"], config)
            bar[i] = s
            reasons[i] = r
            features[i] = pd.Series(s).rolling(method["feature_window_sessions"], min_periods=1).max().to_numpy().astype("uint8")
            for day in np.flatnonzero(present & (s > 0)):
                wi.writerow([sym, meta["days"][day], ("VALID", "SUSPECT", "INVALID")[s[day]], int(r[day]),
                    ";".join(name for bit, name in REASONS.items() if r[day] & bit)])
            for action in actions:
                wc.writerow([sym, action["session"], action["action"], action.get("ratio", action.get("amount")), action.get("action_type"), action.get("quote_gross_at_action")])
            summaries.append({"ticker": sym, "present_bars": int(present.sum()), "valid_bars": int(np.count_nonzero(present & (s == VALID))),
                "suspect_bars": int(np.count_nonzero(present & (s == SUSPECT))), "invalid_present_bars": int(np.count_nonzero(present & (s == INVALID))),
                "missing_sessions": int((~present).sum()), "split_count": sum(a["action"] == "splits" for a in actions),
                "reverse_split_count": sum(a.get("action_type") == "REVERSE_SPLIT" for a in actions),
                "dividend_count": sum(a["action"] == "dividends" for a in actions),
                "mergers_acquisitions_ticker_reuse": "NO_EXHAUSTIVE_HISTORICAL_IDENTITY_ARCHIVE; inception mismatch checked, no absence asserted"})
            if (i + 1) % 250 == 0:
                print("Uniform source-quality audit", i + 1, "/", n, flush=True)
    for a in (bar, features, reasons):
        a.flush()
    del bar, features, reasons
    for filename, dtype in files:
        source = mapped(output / filename, stock_shape, dtype)
        target_path = output / (filename + ".transposed")
        target = mapped(target_path, meta["shape"], dtype, "w+")
        for begin in range(0, d, 128):
            end = min(begin + 128, d)
            target[begin:end] = source[:, begin:end].T
        target.flush()
        del source, target
        os.replace(str(target_path), str(output / filename))
    write(output / "quality-ticker-audit.json", summaries)
    write(output / "quality-definitions.json", {"methodology_sha256": digest(method), "labels": {"0": "VALID", "1": "SUSPECT", "2": "INVALID"},
          "reason_bits": REASONS, "meaning_of_valid": "Passes declared machine checks in a retrospective snapshot; not proof of historical identity/corporate-action completeness"})
    print("Quality audit complete; raw prices unchanged", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    build(args.data, args.analysis, args.output)
