#!/usr/bin/env python3
"""Same frozen event controls, sharing uniform proposals across horizons."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import os
from pathlib import Path

import numpy as np

from common import digest, read, write
from daily_history import mapped
from daily_results import forward_vector, periods
from event_random import summarize
from event_signals import BITS, NAMES, VARIANTS


def draw_group(rng, pool, last, day, targets, spacing, feasible, quality_allowed=None):
    """IID uniform proposals; accept without replacement within each horizon.

    Shared proposals only correlate controls across horizons. For each horizon
    the accepted sequence remains uniform over its allowed, unlocked universe.
    """
    layers, reps = feasible.shape
    selected = np.full((layers, reps, int(max(targets, default=0))), -1, dtype="int32")
    filled = np.zeros((layers, reps), dtype="int32")
    targets = np.asarray(targets, dtype=int)[:, None]
    if not len(pool):
        feasible[targets.repeat(reps, axis=1) > 0] = False
        return selected
    rounds = 0
    while True:
        needs = (filled < targets) & feasible
        pending = np.flatnonzero(np.any(needs, axis=0))
        if not len(pending):
            break
        if rounds >= 4 * selected.shape[2] + 64:
            for h, rep in zip(*np.nonzero(needs)):
                allowed = day - last[h, rep, pool] >= spacing
                if quality_allowed is not None:
                    allowed &= quality_allowed[h, pool]
                available = pool[allowed]
                count = int(targets[h, 0] - filled[h, rep])
                if len(available) < count:
                    feasible[h, rep] = False
                    continue
                chosen = rng.choice(available, count, replace=False)
                selected[h, rep, filled[h, rep]:filled[h, rep] + count] = chosen
                last[h, rep, chosen] = day
                filled[h, rep] += count
            break
        proposals = pool[rng.integers(0, len(pool), size=len(pending))]
        accept = (day - last[:, pending, proposals] >= spacing) & needs[:, pending]
        if quality_allowed is not None:
            accept &= quality_allowed[:, proposals]
        hh, columns = np.nonzero(accept)
        rr, symbols = pending[columns], proposals[columns]
        selected[hh, rr, filled[hh, rr]] = symbols
        last[hh, rr, symbols] = day
        filled[hh, rr] += 1
        rounds += 1
    return selected


def _group(job):
    analysis, root, variant, name, kind = job
    analysis, root = Path(analysis), Path(root)
    method = read(root / "frozen-methodology.json")
    meta = read(root / "event-metadata.json")
    stats = read(root / "event-statistics.json")
    horizons = method["horizons"]
    shape = meta["shape"]
    close = mapped(analysis / "close.f64", shape, "float64")
    after = mapped(analysis / "forward-contiguous.u8", shape, "uint8")
    eligible = mapped(analysis / "eligible.u8", shape, "uint8")
    event = mapped(root / ("events-" + variant + ".u8"), shape, "uint8")
    windows = [mapped(root / ("quality-window-" + str(h) + ".u8"), shape, "uint8") for h in horizons]
    si, vi = NAMES.index(name), VARIANTS.index(variant)
    bit, spacing = BITS[si], 2 if variant == "FULL_RESET" else 20
    iterations = method["randomizations"]
    layers = 1 if kind == "PRIMARY" else len(horizons)
    ranges = periods(meta)
    labels = list(ranges)
    first, final = ranges["full"]
    seed = method["seed"] + 10000 * vi + si + (1000000 if kind == "VALID_MATCHED" else 0)
    rng = np.random.default_rng(seed)
    config = {"algorithm": "shared_uniform_proposals_v1", "kind": kind, "variant": variant, "strategy": name,
              "horizons": horizons, "spacing": spacing, "iterations": iterations, "seed": seed,
              "methodology_sha256": digest(method), "source_statistics_sha256": digest(stats)}
    signature = digest(config)
    folder = root / "random-groups"
    folder.mkdir(exist_ok=True)
    ident = kind + "-" + variant + "-" + name
    filename = folder / (ident + ".npz")
    sums = np.zeros((len(labels), 2, len(horizons), iterations))
    counts = np.zeros_like(sums, dtype="int64")
    feasible = np.ones((layers, iterations), dtype=bool)
    last = np.full((layers, iterations, shape[1]), -100000, dtype="int32")
    begin = first
    if filename.exists():
        with np.load(filename) as cached:
            state = json.loads(str(cached["state_json"].item()))
            if state["signature"] != signature:
                raise ValueError("grouped control checkpoint differs from frozen inputs")
            begin = state["next_day"]
            sums[:], counts[:], feasible[:] = cached["sums"], cached["counts"], cached["feasible"]
            rng.bit_generator.state = state["rng_state"]
            if begin <= final:
                last[:] = cached["last"]

    def checkpoint(next_day):
        payload = {"sums": sums, "counts": counts, "feasible": feasible,
                   "state_json": np.array(json.dumps({"next_day": next_day, "signature": signature, "rng_state": rng.bit_generator.state}, sort_keys=True))}
        if next_day <= final:
            payload["last"] = last
        temp = folder / (ident + ".tmp.npz")
        # Uncompressed atomic checkpoints avoid spending more CPU compressing
        # the last-draw book than running the simulation. Final checkpoints
        # retain all numeric results but need no already-finished draw book.
        np.savez(temp, **payload)
        os.replace(str(temp), str(filename))

    for day in range(begin, final + 1):
        source = (event[day] & bit) != 0
        if not np.any(source):
            continue
        pool = np.flatnonzero(eligible[day])
        quality = np.asarray([window[day] == 0 for window in windows])
        returns = np.asarray([forward_vector(close, after, day, h, final) for h in horizons])
        if kind == "PRIMARY":
            targets = np.asarray([int(source.sum())])
            allowed = None
        else:
            allowed = quality & np.isfinite(returns)
            targets = np.count_nonzero(allowed & source[None, :], axis=1)
        chosen = draw_group(rng, pool, last, day, targets, spacing, feasible, allowed)
        for hi, h in enumerate(horizons):
            li = 0 if kind == "PRIMARY" else hi
            k = int(targets[li])
            if not k:
                continue
            picked = chosen[li, :, :k]
            safe = np.maximum(picked, 0)
            values = returns[hi, safe]
            observed = (picked >= 0) & np.isfinite(values) & feasible[li, :, None]
            clean = observed & quality[hi, safe]
            for pi, label in enumerate(labels):
                start, end = ranges[label]
                if start <= day and day + h <= end:
                    for qi, mask in enumerate((observed, clean)):
                        sums[pi, qi, hi] += np.where(mask, values, 0).sum(axis=1)
                        counts[pi, qi, hi] += mask.sum(axis=1)
        if (day - first + 1) % 1000 == 0 or day == final:
            checkpoint(day + 1)
            print("Grouped event control", ident, day - first + 1, "/", final - first + 1, flush=True)
    checkpoint(final + 1)
    result = {"config": config, "horizons": {}}
    for hi, h in enumerate(horizons):
        li = 0 if kind == "PRIMARY" else hi
        hf = feasible[li]
        current = {"feasible_replicates": int(hf.sum()), "infeasible_replicates": int((~hf).sum()),
                   "quality_conditioned_on_future": kind == "VALID_MATCHED", "periods": {}}
        for pi, label in enumerate(labels):
            current["periods"][label] = {}
            for qi, panel in enumerate(("ALL", "VALID_ONLY")):
                if kind == "VALID_MATCHED" and panel != "VALID_ONLY":
                    continue
                actual = stats["statistics"][variant][name][str(h)]["ALL_WINDOWS"][label][panel]
                use = hf & (counts[pi, qi, hi] > 0)
                means = sums[pi, qi, hi, use] / counts[pi, qi, hi, use]
                ns = counts[pi, qi, hi, use]
                row = summarize(means, actual["mean"])
                row.update(strategy_mean=actual["mean"], strategy_median=actual["median"], strategy_n=actual["n"],
                    matched_selected_events=actual["selected_events"] if kind == "PRIMARY" else actual["n"],
                    random_observed_n_min=int(ns.min()) if len(ns) else None,
                    random_observed_n_median=float(np.median(ns)) if len(ns) else None,
                    random_observed_n_max=int(ns.max()) if len(ns) else None,
                    valid_estimates=int(use.sum()), attempted=iterations)
                if kind == "VALID_MATCHED" and len(ns) and np.any(ns != actual["n"]):
                    raise AssertionError("quality-matched random event counts differ")
                current["periods"][label][panel] = row
        result["horizons"][str(h)] = current
    write(folder / (ident + ".json"), result)
    print("Grouped event control completed", ident, flush=True)
    return result


def run(analysis, output, workers=2):
    root = Path(output)
    method = read(root / "frozen-methodology.json")
    plan = {"algorithm": "shared_uniform_proposals_v1", "methodology_sha256": digest(method),
            "changes": "Computational sharing across horizons only; same uniform marginal controls, spacing, dates, counts and QC. No strategy or quality rule changed.",
            "initial_engine_attempt": "Preserved under random-cases; not mixed with final grouped results"}
    destination = root / "random-engine.json"
    if destination.exists() and read(destination) != plan:
        raise ValueError("random engine plan changed")
    write(destination, plan)
    results = {"methodology_sha256": digest(method), "engine": plan,
               "primary_no_future_quality_selection": {}, "valid_matched_retrospective_sensitivity": {}}
    jobs = [(str(analysis), str(root), v, n, k) for k in ("PRIMARY", "VALID_MATCHED") for v in VARIANTS for n in NAMES]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for group in pool.map(_group, jobs, chunksize=1):
            c = group["config"]
            target = results["primary_no_future_quality_selection" if c["kind"] == "PRIMARY" else "valid_matched_retrospective_sensitivity"]
            target.setdefault(c["variant"], {})[c["strategy"]] = group["horizons"]
            write(root / "event-random-controls.json", results)
    return results


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--analysis", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--workers", type=int, default=2)
    a = p.parse_args()
    run(a.analysis, a.output, a.workers)
