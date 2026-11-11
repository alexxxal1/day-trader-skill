#!/usr/bin/env python3
"""Forward-return measurement and seeded daily controls, entirely offline."""
import argparse
import csv
import gzip
import hashlib
import json
import math
import os
from pathlib import Path

import numpy as np

from common import digest, read, write
from daily_history import BITS, HORIZONS, NAMES, continuous_runs, mapped


def periods(meta):
    first, last = meta["test_first_index"], meta["test_last_index"]
    split = first + math.floor((last - first + 1) * .7)
    return {"full": (first, last), "in_sample": (first, split - 1), "out_of_sample": (split, last)}


def forward_vector(close, after, day, horizon, last):
    """Outcomes only; never consulted to select the signal or random ticker."""
    result = np.full(close.shape[1], np.nan)
    if day + horizon <= last:
        valid = (after[day] >= horizon) & np.isfinite(close[day]) & (close[day] > 0)
        result[valid] = (close[day + horizon, valid] / close[day, valid] - 1) * 100
    return result


def block_interval(sums, counts, horizon, seed, iterations=1000):
    """Resample whole signal dates in moving blocks, preserving cross-section."""
    n = len(sums)
    if n < 2 * horizon or not counts.sum():
        return None
    s = np.r_[0., np.cumsum(sums)]
    c = np.r_[0, np.cumsum(counts)]
    block_s, block_c = s[horizon:] - s[:-horizon], c[horizon:] - c[:-horizon]
    rng = np.random.default_rng(seed)
    indices = rng.integers(0, len(block_s), size=(iterations, math.ceil(n / horizon)))
    denominator = block_c[indices].sum(axis=1)
    valid = denominator > 0
    estimates = block_s[indices].sum(axis=1)[valid] / denominator[valid]
    return np.quantile(estimates, [.025, .975]).tolist() if len(estimates) else None


def source_statistics(analysis, output):
    analysis, output = Path(analysis), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    meta = read(analysis / "matrix-metadata.json")
    shape = meta["shape"]
    close = mapped(analysis / "close.f64", shape, "float64")
    eligible = mapped(analysis / "eligible.u8", shape, "uint8")
    signals = mapped(analysis / "signals.u8", shape, "uint8")
    after = mapped(analysis / "forward-contiguous.u8", shape, "uint8")
    spy = read(analysis / "benchmarks.json")["SPY"]
    spy_close = np.asarray(spy["close"], dtype=float)
    _, spy_after = continuous_runs(np.asarray(spy["valid"], dtype=bool))
    ranges = periods(meta)
    study = {"layer": "DAILY_HISTORY", "periods": {}, "flags": meta["flags"],
             "signal_counts": {}, "daily_cohort": {}, "return_units": "percentage points",
             "costs": "Gross descriptive price returns: all strategies and controls have zero execution costs; no trade/entry invented",
             "is_fraction": .7, "selection": "Same point-in-time data-eligible population for all five variants; native strategy parameters unchanged",
             "sample_unit": "one daily-close observation per ticker, date and strategy; repeated/overlapping observations are dependent"}
    day_counts = eligible.sum(axis=1)
    start, end = ranges["full"]
    study["daily_cohort"] = {"requested": len(meta["universe"]),
        "used_stocks": int(np.count_nonzero(np.any(eligible[start:end + 1], axis=0))),
        "minimum": int(day_counts[start:end + 1].min()), "maximum": int(day_counts[start:end + 1].max()),
        "median": float(np.median(day_counts[start:end + 1])), "sessions": end - start + 1,
        "start": meta["days"][start], "end": meta["days"][end]}
    for name, bit in zip(NAMES, BITS):
        mask = (signals[start:end + 1] & bit) != 0
        study["signal_counts"][name] = {"signals": int(mask.sum()),
            "stocks": int(np.count_nonzero(np.any(mask, axis=0))),
            "signal_days": int(np.count_nonzero(np.any(mask, axis=1)))}
    with (output / "daily-cohort-and-signal-counts.csv").open("w", newline="") as stream:
        w = csv.writer(stream)
        w.writerow(["session", "eligible_stocks"] + list(NAMES))
        for day in range(start, end + 1):
            w.writerow([meta["days"][day], int(day_counts[day])] + [int(np.count_nonzero(signals[day] & bit)) for bit in BITS])
    for label, (first, last) in ranges.items():
        study["periods"][label] = {"start": meta["days"][first], "end": meta["days"][last], "strategies": {name: {} for name in NAMES}}
        count_days = last - first + 1
        for h in HORIZONS:
            returns = np.full((count_days, shape[1]), np.nan)
            spy_returns = np.full(count_days, np.nan)
            for relative, day in enumerate(range(first, last + 1)):
                returns[relative] = forward_vector(close, after, day, h, last)
                if day + h <= last and spy["valid"][day] and spy_after[day] >= h:
                    spy_returns[relative] = (spy_close[day + h] / spy_close[day] - 1) * 100
            for name, bit in zip(NAMES, BITS):
                mask = (signals[first:last + 1] & bit) != 0
                observable = mask & np.isfinite(returns)
                values = returns[observable]
                n = len(values)
                total = int(mask.sum())
                sums = np.nansum(np.where(observable, returns, 0), axis=1)
                counts = observable.sum(axis=1)
                paired = observable & np.isfinite(spy_returns[:, None])
                paired_n = int(paired.sum())
                excess_sum = float(np.nansum(np.where(paired, returns - spy_returns[:, None], 0)))
                censored = int(mask[max(0, count_days - h):].sum())
                result = {"signals": total, "n": n, "right_or_boundary_censored": censored,
                    "missing_forward_history": total - n - censored,
                    "mean_return_pct": float(values.mean()) if n else None,
                    "median_return_pct": float(np.median(values)) if n else None,
                    "win_rate": float(np.count_nonzero(values > 0) / n) if n else None,
                    "loss_rate": float(np.count_nonzero(values < 0) / n) if n else None,
                    "excess_return_vs_spy_pct": excess_sum / paired_n if paired_n else None,
                    "spy_pairs": paired_n,
                    "distinct_stocks": int(np.count_nonzero(np.any(observable, axis=0))),
                    "distinct_signal_days": int(np.count_nonzero(counts)),
                    "mean_ci95_moving_blocks": block_interval(sums, counts, h, 20261109),
                    "flags": (["LOW_SAMPLE_CONFIDENCE"] if n < 30 else []) + ["DEPENDENT_OVERLAPPING_OBSERVATIONS", "EXPLORATORY_RESEARCH"]}
                study["periods"][label]["strategies"][name][str(h)] = result
            del returns
            print("Daily forward statistics", label, h, "sessions", flush=True)
        write(output / "daily-source-statistics.json", study)
    # Compact per-observation audit; all horizon outcomes are reproducible from
    # the immutable close/continuity matrices and raw archives.
    with gzip.open(output / "signal-ledger.csv.gz", "wt", newline="") as stream:
        w = csv.writer(stream)
        w.writerow(["session", "ticker", "daily_close_snapshot_basis", "strategy_bits", "bit_legend"])
        legend = ";".join(str(b) + "=" + n for n, b in zip(NAMES, BITS))
        for day in range(start, end + 1):
            for i in np.flatnonzero(signals[day]):
                w.writerow([meta["days"][day], meta["universe"][i], float(close[day, i]), int(signals[day, i]), legend])
    return study


def random_controls(analysis, output, iterations=1000, seed=20261109):
    """Exact uniform random ticker subsets, same signal dates/counts/horizons.

    No conditioning on future availability: missing outcomes remain missing.
    Every draw keeps the same number of selected observations. Means use the
    available outcomes under exactly the same policy as strategy means; the
    resulting observed-sample counts and missing fractions are reported.
    This is a signal-quality control, not an executable investment portfolio.
    """
    analysis, output = Path(analysis), Path(output)
    meta = read(analysis / "matrix-metadata.json")
    shape = meta["shape"]
    close = mapped(analysis / "close.f64", shape, "float64")
    eligible = mapped(analysis / "eligible.u8", shape, "uint8")
    signals = mapped(analysis / "signals.u8", shape, "uint8")
    after = mapped(analysis / "forward-contiguous.u8", shape, "uint8")
    source = read(output / "daily-source-statistics.json")
    ranges = periods(meta)
    labels = list(ranges)
    state_path = output / "random-state.json"
    sums_path = output / "random-accumulators.npz"
    study_config = {"algorithm": "uniform_subsets_per_date_v1", "iterations": iterations, "seed": seed,
        "metadata_sha256": digest(meta), "horizons": HORIZONS, "strategy_names": NAMES,
        "method": "same selected counts/date; no replacement within a date; no future observability filter; available-case means, missing outcomes disclosed"}
    config_path = output / "random-config.json"
    # Tuples become JSON lists on disk; compare canonical digests.
    if config_path.exists() and digest(read(config_path)) != digest(study_config):
        raise ValueError("random-control configuration changed; use a new output folder")
    write(config_path, study_config)
    rng = np.random.default_rng(seed)
    accumulator_shape = (len(labels), len(NAMES), len(HORIZONS), iterations)
    sums = np.zeros(accumulator_shape)
    counts = np.zeros(accumulator_shape, dtype="int64")
    if sums_path.exists():
        with np.load(sums_path) as cached:
            state = json.loads(str(cached["state_json"].item())) if "state_json" in cached.files else read(state_path)
            if state["config_sha256"] != digest(study_config):
                raise ValueError("checkpoint does not match frozen random controls")
            sums[:] = cached["sums"]
            counts[:] = cached["counts"]
        rng.bit_generator.state = state["rng_state"]
        next_day = state["next_day"]
    else:
        next_day = meta["test_first_index"]
    start, last = ranges["full"]

    def save_checkpoint(next_index):
        state = {"next_day": next_index, "rng_state": rng.bit_generator.state,
                 "config_sha256": digest(study_config)}
        temporary = output / "random-accumulators.tmp.npz"
        # Commit arrays, cursor and RNG together so an interruption cannot
        # resume new sums with an older RNG state and double-count dates.
        np.savez_compressed(temporary, sums=sums, counts=counts, state_json=np.array(json.dumps(state, sort_keys=True)))
        os.replace(str(temporary), str(sums_path))
        write(state_path, state)

    for day in range(next_day, last + 1):
        pool = np.flatnonzero(eligible[day])
        ks = np.array([np.count_nonzero(signals[day] & bit) for bit in BITS], dtype=int)
        maximum = int(ks.max())
        if maximum:
            # A shared random permutation couples comparisons fairly. Every
            # strategy's first k positions are a uniform k-subset of the pool.
            picks = np.asarray([rng.choice(pool, size=maximum, replace=False) for _ in range(iterations)])
            for hi, h in enumerate(HORIZONS):
                outcomes = forward_vector(close, after, day, h, last)[picks]
                known = np.isfinite(outcomes)
                prefix_sums = np.cumsum(np.where(known, outcomes, 0), axis=1)
                prefix_counts = np.cumsum(known, axis=1)
                for si, k in enumerate(ks):
                    if not k:
                        continue
                    for pi, label in enumerate(labels):
                        begin, finish = ranges[label]
                        if begin <= day and day + h <= finish:
                            sums[pi, si, hi] += prefix_sums[:, k - 1]
                            counts[pi, si, hi] += prefix_counts[:, k - 1]
        if (day - start + 1) % 100 == 0 or day == last:
            save_checkpoint(day + 1)
            print("Daily random controls", day - start + 1, "/", last - start + 1, "dates x", iterations, flush=True)
    if next_day > last:
        save_checkpoint(next_day)
    results = {}
    for pi, label in enumerate(labels):
        results[label] = {}
        for si, name in enumerate(NAMES):
            results[label][name] = {}
            for hi, h in enumerate(HORIZONS):
                actual = source["periods"][label]["strategies"][name][str(h)]
                valid = counts[pi, si, hi] > 0
                values = sums[pi, si, hi, valid] / counts[pi, si, hi, valid]
                ns = counts[pi, si, hi, valid]
                result = {"attempted": iterations, "valid_mean_estimates": int(valid.sum()),
                    "matched_selected_observations": actual["signals"],
                    "strategy_observed_outcomes": actual["n"],
                    "random_observed_outcomes_min": int(ns.min()) if len(ns) else None,
                    "random_observed_outcomes_median": float(np.median(ns)) if len(ns) else None,
                    "random_observed_outcomes_max": int(ns.max()) if len(ns) else None,
                    "median_random_mean_return_pct": float(np.median(values)) if len(values) else None,
                    "p05_random_mean_return_pct": float(np.quantile(values, .05)) if len(values) else None,
                    "p95_random_mean_return_pct": float(np.quantile(values, .95)) if len(values) else None,
                    "fraction_random_better": float(np.mean(values > actual["mean_return_pct"])) if len(values) and actual["mean_return_pct"] is not None else None,
                    "flags": ["SIGNAL_CONTROL_NOT_EXECUTABLE_PORTFOLIO", "AVAILABLE_CASE_MEANS_WITH_MISSING_COUNTS", "NO_FUTURE_SELECTION"]}
                results[label][name][str(h)] = result
    write(output / "random-controls.json", results)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("statistics", "random", "all"))
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--iterations", type=int, default=1000)
    args = parser.parse_args()
    if args.command in ("statistics", "all"):
        source_statistics(args.analysis, args.output)
    if args.command in ("random", "all"):
        random_controls(args.analysis, args.output, args.iterations)
