#!/usr/bin/env python3
"""Matched event controls with spacing, identical QC and atomic case checkpoints."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import os
from pathlib import Path

import numpy as np

from common import digest, read, write
from daily_history import mapped
from daily_results import forward_vector, periods
from event_signals import BITS, NAMES, VARIANTS


def draw_spaced(rng, pool, last_draw, day, count, spacing, feasible):
    """Uniform draws without replacement among each replicate's unlocked names."""
    result = np.full((len(feasible), count), -1, dtype="int32")
    if count and not len(pool):
        feasible[:] = False
        return result
    for slot in range(count):
        pending = np.flatnonzero(feasible)
        attempts = 0
        while len(pending):
            if attempts >= 8:
                # Direct uniform selection is equivalent to rejection sampling
                # and avoids an unbounded loop when little capacity remains.
                for row in pending:
                    allowed = pool[day - last_draw[row, pool] >= spacing]
                    if not len(allowed):
                        feasible[row] = False
                    else:
                        symbol = int(rng.choice(allowed))
                        result[row, slot] = symbol
                        last_draw[row, symbol] = day
                break
            proposed = pool[rng.integers(0, len(pool), size=len(pending))]
            accepted = day - last_draw[pending, proposed] >= spacing
            rows, symbols = pending[accepted], proposed[accepted]
            result[rows, slot] = symbols
            last_draw[rows, symbols] = day
            pending = pending[~accepted]
            attempts += 1
    return result


def summarize(means, actual_mean):
    values = means[np.isfinite(means)]
    if not len(values):
        return {"random_median_mean": None, "random_p05": None, "random_p95": None, "fraction_random_better": None, "strategy_percentile": None}
    return {"random_median_mean": float(np.median(values)), "random_p05": float(np.quantile(values, .05)),
            "random_p95": float(np.quantile(values, .95)),
            "fraction_random_better": float(np.mean(values > actual_mean)) if actual_mean is not None else None,
            "strategy_percentile": float(100 * (np.mean(values < actual_mean) + .5 * np.mean(values == actual_mean))) if actual_mean is not None else None}


def _case(job):
    analysis, root, variant, strategy, horizon, kind = job
    analysis, root = Path(analysis), Path(root)
    method = read(root / "frozen-methodology.json")
    meta = read(root / "event-metadata.json")
    stats = read(root / "event-statistics.json")
    shape = meta["shape"]
    close = mapped(analysis / "close.f64", shape, "float64")
    after = mapped(analysis / "forward-contiguous.u8", shape, "uint8")
    eligible = mapped(analysis / "eligible.u8", shape, "uint8")
    events = mapped(root / ("events-" + variant + ".u8"), shape, "uint8")
    quality = mapped(root / ("quality-window-" + str(horizon) + ".u8"), shape, "uint8")
    bit = BITS[NAMES.index(strategy)]
    spacing = 2 if variant == "FULL_RESET" else 20
    iterations = method["randomizations"]
    ranges = periods(meta)
    labels = list(ranges)
    first, final = ranges["full"]
    case_id = kind + "-" + variant + "-" + strategy + "-" + str(horizon)
    folder = root / "random-cases"
    folder.mkdir(exist_ok=True)
    filename = folder / (case_id + ".npz")
    config = {"kind": kind, "variant": variant, "strategy": strategy, "horizon": horizon,
              "spacing": spacing, "iterations": iterations, "method_sha256": digest(method),
              "event_metadata_sha256": digest(meta), "source_statistics_sha256": digest(stats)}
    config_hash = digest(config)
    seed = method["seed"] + 10000 * VARIANTS.index(variant) + 100 * horizon + NAMES.index(strategy) + (1000000 if kind == "VALID_MATCHED" else 0)
    rng = np.random.default_rng(seed)
    sums = np.zeros((len(labels), 2, iterations))
    counts = np.zeros((len(labels), 2, iterations), dtype="int64")
    feasible = np.ones(iterations, dtype=bool)
    last_draw = np.full((iterations, shape[1]), -100000, dtype="int32")
    begin_day = first
    if filename.exists():
        with np.load(filename) as cached:
            state = json.loads(str(cached["state_json"].item()))
            if state["config_sha256"] != config_hash:
                raise ValueError("random case configuration changed: " + case_id)
            sums[:], counts[:], feasible[:] = cached["sums"], cached["counts"], cached["feasible"]
            begin_day = state["next_day"]
            rng.bit_generator.state = state["rng_state"]
            if begin_day <= final:
                last_draw[:] = cached["last_draw"]

    def checkpoint(next_day):
        state = {"next_day": next_day, "rng_state": rng.bit_generator.state, "config_sha256": config_hash}
        temp = folder / (case_id + ".tmp.npz")
        payload = dict(sums=sums, counts=counts, feasible=feasible, state_json=np.array(json.dumps(state, sort_keys=True)))
        if next_day <= final:
            payload["last_draw"] = last_draw
        np.savez_compressed(temp, **payload)
        os.replace(str(temp), str(filename))

    for day in range(begin_day, final + 1):
        source_mask = (events[day] & bit) != 0
        returns = forward_vector(close, after, day, horizon, final)
        pool_mask = eligible[day].astype(bool)
        if kind == "VALID_MATCHED":
            # This explicitly retrospective control matches the retained VALID
            # sample exactly. It is never described as a point-in-time entry.
            good = (quality[day] == 0) & np.isfinite(returns)
            pool_mask &= good
            source_mask &= good
        n = int(source_mask.sum())
        if n:
            chosen = draw_spaced(rng, np.flatnonzero(pool_mask), last_draw, day, n, spacing, feasible)
            safe = np.maximum(chosen, 0)
            values = returns[safe]
            known = np.isfinite(values) & (chosen >= 0) & feasible[:, None]
            valid_quality = known & (quality[day, safe] == 0)
            for pi, label in enumerate(labels):
                start, end = ranges[label]
                if start <= day and day + horizon <= end:
                    for qi, mask in enumerate((known, valid_quality)):
                        sums[pi, qi] += np.where(mask, values, 0).sum(axis=1)
                        counts[pi, qi] += mask.sum(axis=1)
        if (day - first + 1) % 500 == 0 or day == final:
            checkpoint(day + 1)
    result = {"case": config, "seed": seed, "periods": {}, "feasible_replicates": int(feasible.sum()),
              "infeasible_replicates": int((~feasible).sum()), "quality_conditioned_on_future": kind == "VALID_MATCHED"}
    for pi, label in enumerate(labels):
        result["periods"][label] = {}
        for qi, panel in enumerate(("ALL", "VALID_ONLY")):
            if kind == "VALID_MATCHED" and panel != "VALID_ONLY":
                continue
            source = stats["statistics"][variant][strategy][str(horizon)]["ALL_WINDOWS"][label][panel]
            usable = feasible & (counts[pi, qi] > 0)
            means = sums[pi, qi, usable] / counts[pi, qi, usable]
            row = summarize(means, source["mean"])
            ns = counts[pi, qi, usable]
            row.update(strategy_mean=source["mean"], strategy_median=source["median"], strategy_n=source["n"],
                matched_selected_events=source["selected_events"] if kind == "PRIMARY" else source["n"],
                random_observed_n_min=int(ns.min()) if len(ns) else None,
                random_observed_n_median=float(np.median(ns)) if len(ns) else None,
                random_observed_n_max=int(ns.max()) if len(ns) else None,
                valid_estimates=len(means), attempted=iterations)
            if kind == "VALID_MATCHED" and len(ns) and np.any(ns != source["n"]):
                raise AssertionError("VALID control failed to match source event counts exactly")
            result["periods"][label][panel] = row
    write(folder / (case_id + ".json"), result)
    print("Event random control", case_id, "feasible", int(feasible.sum()), flush=True)
    return result


def run(analysis, output, workers=2):
    output = Path(output)
    method = read(output / "frozen-methodology.json")
    jobs = [(str(analysis), str(output), variant, name, h, kind)
            for kind in ("PRIMARY", "VALID_MATCHED") for variant in VARIANTS for name in NAMES for h in method["horizons"]]
    results = {"methodology_sha256": digest(method), "primary_no_future_quality_selection": {}, "valid_matched_retrospective_sensitivity": {}}
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(_case, jobs, chunksize=1):
            case = result["case"]
            destination = results["primary_no_future_quality_selection" if case["kind"] == "PRIMARY" else "valid_matched_retrospective_sensitivity"]
            destination.setdefault(case["variant"], {}).setdefault(case["strategy"], {})[str(case["horizon"])] = result
            write(output / "event-random-controls.json", results)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    run(args.analysis, args.output, args.workers)
