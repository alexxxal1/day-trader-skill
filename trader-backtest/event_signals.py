#!/usr/bin/env python3
"""Postprocess unchanged native states into confirmed, deduplicated events."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import csv
import gzip
from pathlib import Path

import numpy as np

from common import digest, read, write
from daily_history import mapped, native_decisions

NAMES = ("MINERVINI", "BREAKOUT_SETUP_READY", "CONFLUENCE_SETUP_READY")
BITS = (1, 4, 16)
VARIANTS = ("FULL_RESET", "COOLDOWN20")


def previous_state_label(strategy, flags):
    if strategy == "MINERVINI":
        return "PASS" if flags & 1 else "FAIL"
    q = "SETUP_READY" if flags & 4 else "CANDIDATE" if flags & 2 else "FAILED"
    if strategy == "CONFLUENCE_SETUP_READY":
        return "NO_SETUP_CONFLUENCE|BREAKOUT=" + q
    return q


def extract_events(flags, eligible, cooldown=20):
    """Unknown data never rearms a setup; ongoing activity never retriggers."""
    flags, eligible = np.asarray(flags), np.asarray(eligible, dtype=bool)
    index = np.arange(len(flags))
    unknown_at = np.maximum.accumulate(np.where(eligible, -1, index))
    previous_known = np.r_[False, eligible[:-1]]
    reset_conditions = ((flags & 1) == 0,
                        (flags & (2 | 4)) == 0,
                        ((flags & 1) == 0) | ((flags & (2 | 4)) == 0))
    full, cooled = np.zeros(len(flags), dtype="uint8"), np.zeros(len(flags), dtype="uint8")
    diagnostics = []
    anchors = {}
    for name, bit, reset in zip(NAMES, BITS, reset_conditions):
        active = (flags & bit) != 0
        previous_active = np.r_[False, active[:-1]]
        transitions = eligible & previous_known & ~previous_active & active
        left_censored = eligible & ~previous_known & active
        reset_at = np.maximum.accumulate(np.where(eligible & reset, index, -1))
        # A left-censored active state consumes its episode without creating
        # a false event. A data gap invalidates an earlier arming observation.
        consumed = np.flatnonzero(transitions | left_censored)
        ids = reset_at[consumed]
        first_in_episode = np.r_[True, ids[1:] != ids[:-1]]
        armed = (ids >= 0) & (ids > unknown_at[consumed])
        full_indices = consumed[first_in_episode & armed & transitions[consumed]]
        full[full_indices] |= bit
        last = -cooldown
        cooled_indices = []
        for d in np.flatnonzero(transitions):
            if d - last >= cooldown:
                cooled_indices.append(d)
                last = int(d)
        cooled[np.asarray(cooled_indices, dtype=int)] |= bit
        anchors[name] = reset_at
        diagnostics.append({"strategy": name, "confirmed_activation_transitions": int(transitions.sum()),
            "left_censored_active_episodes": int(left_censored.sum()), "full_reset_events": len(full_indices),
            "cooldown20_events": len(cooled_indices)})
    return full, cooled, diagnostics, anchors


def nonoverlap_indices(days, horizon):
    """Greedy selection uses dates only, never quality or future returns."""
    keep, last = [], -horizon
    for d in days:
        if d - last >= horizon:
            keep.append(int(d))
            last = int(d)
    return np.asarray(keep, dtype=int)


def _ticker(job):
    analysis, i = job
    analysis = Path(analysis)
    meta = read(analysis / "matrix-metadata.json")
    shape, first = meta["shape"], meta["test_first_index"]
    sym = meta["universe"][i]
    eligible = mapped(analysis / "eligible.u8", shape, "uint8")[:, i].astype(bool)
    states = mapped(analysis / "signals.u8", shape, "uint8")[:, i].copy()
    if np.any(eligible[:first]):
        # Recover pre-study episode state. The old daily output deliberately
        # suppressed warmup signals, while its native ranks were retained.
        ranks = mapped(analysis / "ranks.f64", (4,) + tuple(shape), "float64")[:, :first, i]
        with np.load(analysis / "stock-cache" / (sym + ".npz")) as cache:
            warmup, _ = native_decisions(cache["c"][:first], cache["h"][:first], cache["l"][:first], cache["v"][:first],
                eligible[:first], ranks, meta["days"][:first], factor=cache["factor"][:first])
        states[:first] = warmup
    a, b, counts, anchors = extract_events(states, eligible)
    ledger = []
    for variant, events in zip(VARIANTS, (a, b)):
        for name, bit in zip(NAMES, BITS):
            prior = None
            for d in np.flatnonzero(events & bit):
                if d >= first:
                    before = states[d - 1]
                    prior_state = previous_state_label(name, before)
                    anchor = int(anchors[name][d])
                    ledger.append([variant, name, sym, meta["days"][d], int(d), prior_state,
                        meta["days"][anchor] if anchor >= 0 else None,
                        meta["days"][prior] if prior is not None else None])
                prior = int(d)
    a[:first], b[:first] = 0, 0
    summary = {"ticker": sym, "state_counters_including_warmup": counts,
               "daily_observations": {name: int(np.count_nonzero(states[first:] & bit)) for name, bit in zip(NAMES, BITS)},
               "event_counts": {v: {name: int(np.count_nonzero(ev & bit)) for name, bit in zip(NAMES, BITS)} for v, ev in zip(VARIANTS, (a, b))}}
    return i, a, b, ledger, summary


def build(analysis, output, workers=2):
    analysis, output = Path(analysis), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    meta = read(analysis / "matrix-metadata.json")
    method = read(output / "frozen-methodology.json")
    if method != read(Path(__file__).parent / "event-methodology.json"):
        raise ValueError("methodology changed after freeze")
    a = mapped(output / "events-FULL_RESET.u8", meta["shape"], "uint8", "w+")
    b = mapped(output / "events-COOLDOWN20.u8", meta["shape"], "uint8", "w+")
    a[:], b[:] = 0, 0
    summaries = []
    with gzip.open(output / "event-ledger.csv.gz", "wt", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["variant", "strategy", "ticker", "session", "session_index", "previous_native_state", "last_full_reset_session", "previous_accepted_event_session"])
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for n, (i, aa, bb, ledger, summary) in enumerate(pool.map(_ticker, [(str(analysis), i) for i in range(meta["shape"][1])], chunksize=4), 1):
                a[:, i], b[:, i] = aa, bb
                writer.writerows(ledger)
                summaries.append(summary)
                if n % 500 == 0:
                    print("Event extraction", n, "/", meta["shape"][1], flush=True)
    a.flush()
    b.flush()
    write(output / "event-count-audit.json", summaries)
    write(output / "event-metadata.json", dict(meta, event_methodology_sha256=digest(method), source_analysis=str(analysis)))
    print("Confirmed event datasets written", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    build(args.analysis, args.output, args.workers)
