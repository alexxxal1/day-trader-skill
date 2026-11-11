#!/usr/bin/env python3
"""Distribution, overlap and chronological holdout statistics for fixed events."""
import argparse
from pathlib import Path

import numpy as np

from common import digest, read, write
from daily_history import mapped
from daily_results import block_interval, periods
from data_quality import VALID, SUSPECT, INVALID
from event_signals import BITS, NAMES, VARIANTS


def distribution(values):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    n = len(values)
    if not n:
        return {"n": 0, "mean": None, "median": None, "win_rate": None, "quantiles": {}, "shares": {}, "tail": {}}
    quantiles = {str(p): float(v) for p, v in zip((10, 25, 50, 75, 90, 95, 99), np.quantile(values, [.1, .25, .5, .75, .9, .95, .99]))}
    k = max(1, int(np.ceil(n * .01)))
    top = np.partition(values, n - k)[-k:]
    top_sum = float(top.sum())
    positive = float(values[values > 0].sum())
    return {"n": n, "mean": float(values.mean()), "median": float(np.median(values)),
        "win_rate": float(np.mean(values > 0)), "loss_rate": float(np.mean(values < 0)),
        "quantiles": quantiles,
        "shares": {"gt_plus5": float(np.mean(values > 5)), "gt_plus10": float(np.mean(values > 10)),
                   "lt_minus5": float(np.mean(values < -5)), "lt_minus10": float(np.mean(values < -10))},
        "tail": {"largest_one_percent_n": k, "top_one_percent_mean_contribution_pp": top_sum / n,
                 "remaining_mean_contribution_pp": float(values.sum() - top_sum) / n,
                 "top_one_percent_share_of_gross_positive_returns": float(top[top > 0].sum()) / positive if positive else None},
        "minimum": float(values.min()), "maximum": float(values.max())}


def greedy_nonoverlap(days, assets, horizon):
    keep = np.zeros(len(days), dtype=bool)
    last_asset, last_day = -1, -horizon
    for idx in np.argsort(assets, kind="stable"):
        a, day = int(assets[idx]), int(days[idx])
        if a != last_asset:
            last_asset, last_day = a, -horizon
        if day - last_day >= horizon:
            keep[idx] = True
            last_day = day
    return keep


def overlap_count(days, assets, horizon):
    overlap, last_asset, last_day = 0, -1, -horizon
    for idx in np.argsort(assets, kind="stable"):
        a, day = int(assets[idx]), int(days[idx])
        if a == last_asset and day - last_day < horizon:
            overlap += 1
        last_asset, last_day = a, day
    return overlap


def build_quality_windows(output, shape, horizons):
    output = Path(output)
    bar = mapped(output / "quality-bars.u8", shape, "uint8")
    feature = mapped(output / "quality-features.u8", shape, "uint8")
    d = shape[0]
    for h in horizons:
        window = mapped(output / ("quality-window-" + str(h) + ".u8"), shape, "uint8", "w+")
        window[:] = feature
        for k in range(1, h + 1):
            window[:-k] = np.maximum(window[:-k], bar[k:])
        window[d - h:] = INVALID
        window.flush()
        print("Quality windows", h, "sessions", flush=True)


def outcomes(close, after, days, assets, horizon, last):
    values = np.full(len(days), np.nan)
    inside = days + horizon <= last
    idx = np.flatnonzero(inside)
    idx = idx[after[days[idx], assets[idx]] >= horizon]
    values[idx] = (close[days[idx] + horizon, assets[idx]] / close[days[idx], assets[idx]] - 1) * 100
    return values


def statistics(analysis, output):
    analysis, output = Path(analysis), Path(output)
    meta = read(output / "event-metadata.json")
    method = read(output / "frozen-methodology.json")
    if method != read(Path(__file__).parent / "event-methodology.json"):
        raise ValueError("method changed after event extraction")
    shape = meta["shape"]
    build_quality_windows(output, shape, method["horizons"])
    close = mapped(analysis / "close.f64", shape, "float64")
    after = mapped(analysis / "forward-contiguous.u8", shape, "uint8")
    old_daily = mapped(analysis / "signals.u8", shape, "uint8")
    ranges = periods(meta)
    first, last = ranges["full"]
    results = {"methodology_sha256": digest(method), "periods": {k: [meta["days"][a], meta["days"][b]] for k, (a, b) in ranges.items()},
        "event_counts": {}, "statistics": {}, "daily_reference_full_only": {},
        "bootstrap": "1000 moving-block resamples of whole signal dates; block length max(20, forward horizon)",
        "independence": "Events are state-deduplicated, not assumed statistically independent; nonoverlap removes same-ticker window overlap only"}
    for dataset in ("DAILY_REFERENCE",) + VARIANTS:
        matrix = old_daily if dataset == "DAILY_REFERENCE" else mapped(output / ("events-" + dataset + ".u8"), shape, "uint8")
        target = results["daily_reference_full_only"] if dataset == "DAILY_REFERENCE" else results["statistics"].setdefault(dataset, {})
        count_target = results["event_counts"].setdefault(dataset, {})
        for name, bit in zip(NAMES, BITS):
            days, assets = np.nonzero((matrix & bit) != 0)
            within_study = days >= first
            days, assets = days[within_study], assets[within_study]
            count_target[name] = {"observations": len(days), "tickers": int(len(np.unique(assets))), "signal_days": int(len(np.unique(days)))}
            target[name] = {}
            for h in method["horizons"]:
                ret = outcomes(close, after, days, assets, h, last)
                quality = mapped(output / ("quality-window-" + str(h) + ".u8"), shape, "uint8")[days, assets]
                nonoverlap = greedy_nonoverlap(days, assets, h) if dataset != "DAILY_REFERENCE" else None
                scopes = {"ALL_WINDOWS": np.ones(len(days), dtype=bool)}
                if nonoverlap is not None:
                    scopes["NONOVERLAP"] = nonoverlap
                target[name][str(h)] = {}
                for scope, scope_mask in scopes.items():
                    target[name][str(h)][scope] = {}
                    for period, (begin, finish) in ranges.items():
                        if dataset == "DAILY_REFERENCE" and period != "full":
                            continue
                        candidate = scope_mask & (days >= begin) & (days <= finish)
                        complete = candidate & (days + h <= finish)
                        observed = complete & np.isfinite(ret)
                        target[name][str(h)][scope][period] = {}
                        for panel in ("ALL", "VALID_ONLY"):
                            used = observed & ((quality == VALID) if panel == "VALID_ONLY" else True)
                            row = distribution(ret[used])
                            row.update(selected_events=int(candidate.sum()),
                                boundary_censored=int(np.count_nonzero(candidate & ~complete)),
                                numerically_missing=int(np.count_nonzero(complete & ~np.isfinite(ret))),
                                quality_counts={"VALID": int(np.count_nonzero(complete & (quality == VALID))),
                                                "SUSPECT": int(np.count_nonzero(complete & (quality == SUSPECT))),
                                                "INVALID": int(np.count_nonzero(complete & (quality == INVALID)))},
                                used_tickers=int(len(np.unique(assets[used]))), used_signal_days=int(len(np.unique(days[used]))))
                            overlaps = overlap_count(days[used], assets[used], h)
                            row.update(overlapping_windows=overlaps,
                                overlap_fraction=overlaps / row["n"] if row["n"] else None,
                                overlap_definition="same ticker/strategy: previous retained start less than horizon sessions earlier",
                                sample_warning="OVERLAPPING_FORWARD_RETURNS" if overlaps else "CROSS_SECTIONAL_AND_TIME_DEPENDENCE_REMAIN")
                            if scope == "ALL_WINDOWS" and dataset != "DAILY_REFERENCE":
                                sums = np.bincount(days[used] - begin, weights=ret[used], minlength=finish - begin + 1)
                                counts = np.bincount(days[used] - begin, minlength=finish - begin + 1)
                                row["mean_ci95_date_block_bootstrap"] = block_interval(sums, counts, max(20, h), method["seed"], method["bootstrap_samples"])
                            if row["n"] < 30:
                                row["sample_warning"] += ";LOW_SAMPLE_CONFIDENCE"
                            target[name][str(h)][scope][period][panel] = row
                print("Event distribution", dataset, name, h, flush=True)
        write(output / "event-statistics.json", results)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    statistics(args.analysis, args.output)
