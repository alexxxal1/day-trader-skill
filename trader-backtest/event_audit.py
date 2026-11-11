#!/usr/bin/env python3
"""Keep every fixed event/horizon, including suspect, invalid and censored rows."""
import argparse
import csv
import gzip
from pathlib import Path

import numpy as np

from common import read, write
from daily_history import mapped
from daily_results import periods
from event_signals import BITS, NAMES, VARIANTS
from event_statistics import outcomes, greedy_nonoverlap


def build(analysis, output):
    analysis, output = Path(analysis), Path(output)
    meta = read(output / "event-metadata.json")
    method = read(output / "frozen-methodology.json")
    shape = meta["shape"]
    close = mapped(analysis / "close.f64", shape, "float64")
    after = mapped(analysis / "forward-contiguous.u8", shape, "uint8")
    original = mapped(analysis / "signals.u8", shape, "uint8")
    eligible = mapped(analysis / "eligible.u8", shape, "uint8")
    ranges = periods(meta)
    first, last = ranges["full"]
    split = ranges["out_of_sample"][0]
    summaries, extreme_rows = [], 0
    with gzip.open(output / "event-forward-ledger.csv.gz", "wt", newline="") as file, gzip.open(output / "extreme-event-returns.csv.gz", "wt", newline="") as extreme:
        w, wx = csv.writer(file), csv.writer(extreme)
        headers = ["variant", "strategy", "ticker", "signal_session", "horizon", "end_session", "return_pct", "quality",
                   "numerically_observed", "overlaps_previous_event", "greedy_nonoverlap_retained", "signal_period", "crosses_is_oos_boundary"]
        w.writerow(headers)
        wx.writerow(headers)
        total = 0
        for variant in VARIANTS:
            events = mapped(output / ("events-" + variant + ".u8"), shape, "uint8")
            for name, bit in zip(NAMES, BITS):
                days, assets = np.nonzero((events & bit) != 0)
                order = np.argsort(assets, kind="stable")
                previous = np.full(len(days), -100000, dtype=int)
                for left, right in zip(order[:-1], order[1:]):
                    if assets[left] == assets[right]:
                        previous[right] = days[left]
                for h in method["horizons"]:
                    values = outcomes(close, after, days, assets, h, last)
                    q = mapped(output / ("quality-window-" + str(h) + ".u8"), shape, "uint8")[days, assets]
                    retained = greedy_nonoverlap(days, assets, h)
                    overlap = days - previous < h
                    extreme_count = 0
                    for j, (day, asset) in enumerate(zip(days, assets)):
                        end = int(day) + h
                        observed = bool(np.isfinite(values[j]))
                        row = [variant, name, meta["universe"][asset], meta["days"][day], h,
                               meta["days"][end] if end <= last else None,
                               float(values[j]) if observed else None,
                               ("VALID", "SUSPECT", "INVALID")[q[j]] if end <= last else "RIGHT_CENSORED",
                               observed, bool(overlap[j]), bool(retained[j]), "IS" if day < split else "OOS", bool(day < split <= end)]
                        w.writerow(row)
                        total += 1
                        if observed and (1 + values[j] / 100 >= method["quality"]["extreme_forward_review_factor"] or 1 + values[j] / 100 <= 1 / method["quality"]["extreme_forward_review_factor"]):
                            wx.writerow(row)
                            extreme_count += 1
                            extreme_rows += 1
                    summaries.append({"variant": variant, "strategy": name, "horizon": h, "records": len(days),
                                      "extreme_review_records": extreme_count, "raw_overlap_starts": int(overlap.sum())})
                    print("Forward ledger", variant, name, h, flush=True)
    left_censored = {}
    for name, bit in zip(NAMES, BITS):
        count = 0
        for day in range(first, last + 1):
            previous_known = eligible[day - 1].astype(bool) if day else np.zeros(shape[1], dtype=bool)
            count += np.count_nonzero(eligible[day].astype(bool) & ~previous_known & ((original[day] & bit) != 0))
        left_censored[name] = int(count)
    write(output / "forward-ledger-audit.json", {"total_event_horizon_records": total, "extreme_review_records": extreme_rows,
        "summaries": summaries, "left_censored_active_episodes_in_study": left_censored,
        "extremes_removed": 0, "quality_can_create_or_reset_events": False})


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--analysis", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    build(a.analysis, a.output)
