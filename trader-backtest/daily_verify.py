#!/usr/bin/env python3
"""Seeded real-snapshot comparisons against the unchanged scanner functions."""
import argparse
import gzip
import json
from pathlib import Path

import numpy as np

from common import module, read, write
from daily_history import HistoricalClock, mapped


def verify(data, analysis, output, observations=120):
    data, analysis = Path(data), Path(analysis)
    meta = read(analysis / "matrix-metadata.json")
    clock = HistoricalClock(read(data / "calendar.json"))
    mini = module("trader-scanner", "minervini")
    qull = module("trader-scanner", "qullamaggie")
    scan = module("trader-scanner", "scan")
    shape = meta["shape"]
    eligible = mapped(analysis / "eligible.u8", shape, "uint8")
    ranks = mapped(analysis / "ranks.f64", (4,) + tuple(shape), "float64")
    flags = mapped(analysis / "signals.u8", shape, "uint8")
    rng = np.random.default_rng(20261109)
    comparisons, mismatches = [], []
    while len(comparisons) < observations:
        i = int(rng.integers(shape[1]))
        days = np.flatnonzero(eligible[:, i])
        days = days[days >= meta["test_first_index"]]
        if not len(days):
            continue
        day = int(rng.choice(days))
        sym = meta["universe"][i]
        doc = json.loads(gzip.decompress((data / "raw-daily" / (sym + ".json.gz")).read_bytes()))
        series = scan.parse_chart(doc, sym)
        now = clock.close(meta["days"][day])
        values, error = mini.metrics(series, now, clock)
        native_m = mini.evaluate(sym, values, float(ranks[3, day, i]), error)
        bars, q_error = qull.prepare(series, now, clock)
        if q_error:
            raise ValueError("unexpected native data mismatch: " + sym + " " + q_error)
        rank = {p: float(ranks[k, day, i]) for k, p in enumerate(("1M", "3M", "6M"))}
        native_q, _, _, _ = qull.breakout(bars, rank, qull.config())
        expected = (1 if native_m["trend_template"] == "PASS" else 0) | (2 if native_q == "CANDIDATE" else 4 if native_q == "SETUP_READY" else 0)
        if expected & 1 and expected & 2:
            expected |= 8
        if expected & 1 and expected & 4:
            expected |= 16
        row = {"ticker": sym, "session": meta["days"][day], "native_bits": expected, "daily_adapter_bits": int(flags[day, i])}
        comparisons.append(row)
        if expected != int(flags[day, i]):
            mismatches.append(row)
    result = {"seed": 20261109, "observations": observations, "mismatches": mismatches, "comparisons": comparisons,
              "method": "Independent full-history native metrics/prepare/evaluate/breakout against saved daily adapter bits; same point-in-time rank population"}
    write(output, result)
    if mismatches:
        raise AssertionError("Native equivalence mismatches: " + str(mismatches[:5]))
    print("Real-snapshot native equivalence:", observations, "observations, zero mismatches", flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--observations", type=int, default=120)
    args = parser.parse_args()
    verify(args.data, args.analysis, args.output, args.observations)
