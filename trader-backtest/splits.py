"""Fixed chronological partitions. Outcomes crossing a boundary are purged."""
import math


def partitions(days, fraction=0.7, train=80, test=40):
    if not 0 < fraction < 1 or train < 1 or test < 1:
        raise ValueError("invalid chronological split")
    n = math.floor(len(days) * fraction)
    if n < 1 or n >= len(days):
        return {"full": days}, []
    periods = {"full": days, "in_sample": days[:n], "out_of_sample": days[n:]}
    windows = []
    for begin in range(train, len(days), test):
        holdout = days[begin:begin + test]
        if len(holdout) < test:
            break
        windows.append({"train": days[begin - train:begin], "test": holdout})
    return periods, windows


def select_samples(samples, days):
    if not days:
        return []
    selected = []
    for row in samples:
        if days[0] <= row["day"] <= days[-1]:
            if row["end_day"] and row["end_day"] > days[-1]:
                row = dict(row, return_pct=None, excess_return_pct=None, spy_return_pct=None,
                           max_positive_move_pct=None, max_negative_move_pct=None, issue="PURGED_SPLIT_BOUNDARY")
            selected.append(row)
    return selected


def select_plans(plans, days, hold_sessions):
    if not days:
        return []
    # A fixed horizon embargo prevents outcomes leaking across IS/OOS. It is
    # independent of whether a stop would have closed the trade early.
    allowed = set(days[:len(days) - hold_sessions + 1])
    return [p for p in plans if p["day"] in allowed]
