"""Empirical full-trade/path bootstrap; reapply the actual portfolio guards."""
import bisect
import math
import random

import numpy as np

from portfolio_engine import simulate
from portfolio_metrics import drawdowns, streaks

QUANTILES = (1, 5, 10, 25, 50, 75, 90, 95, 99)


def bootstrap_indices(rng, n, block=1):
    if type(n) is not int or n < 1 or type(block) is not int or not 1 <= block <= n:
        raise ValueError("valid sample and block sizes required")
    result = []
    while len(result) < n:
        start = rng.randrange(n - block + 1)
        result.extend(range(start, start + min(block, n - len(result))))
    return result


def shift_trade(source, start, suffix):
    delta = start - source["entry_t"]
    return dict(source, id=source["id"] + "/" + str(suffix), entry_t=start,
                exit_t=source["exit_t"] + delta,
                marks=[[t + delta, mid, fx] for t, mid, fx in source["marks"]])


def block_sample(pool, timeline, indices):
    """Moving blocks of whole sessions, including zero-trade sessions.

    All same-session trades travel together. Complete paths extend across block
    boundaries; fresh admission limits still apply. Session phase is preserved
    on early closes, with time scaled to the destination session length.
    """
    by_day = {}
    for p in pool:
        d = bisect.bisect_right(timeline.starts, p["entry_t"]) - 1
        by_day.setdefault(d, []).append(p)
    out = []
    for dest, original in enumerate(indices):
        def remap(t):
            # Marks/exits at a close belong to that completed session.
            src = bisect.bisect_left(timeline.ends, t)
            src = min(src, len(timeline.days) - 1)
            target = dest + src - original
            fraction = (t - timeline.starts[src]) / (timeline.ends[src] - timeline.starts[src])
            if target >= len(timeline.days):
                return timeline.total + (target - len(timeline.days) + fraction) * 390
            return timeline.starts[target] + fraction * (timeline.ends[target] - timeline.starts[target])
        for p in by_day.get(original, []):
            phase = (p["entry_t"] - timeline.starts[original]) / (timeline.ends[original] - timeline.starts[original])
            entry = timeline.starts[dest] + phase * (timeline.ends[dest] - timeline.starts[dest])
            out.append(dict(p, id=p["id"] + "/block/" + str(dest), entry_t=entry, exit_t=remap(p["exit_t"]),
                            marks=[[remap(t), mid, fx] for t, mid, fx in p["marks"]]))
    return out


def quantiles(values):
    if not len(values):
        return {"P%02d" % p: None for p in QUANTILES}
    return {"P%02d" % p: float(v) for p, v in zip(QUANTILES, np.percentile(values, QUANTILES))}


def summarize(capital, dd, daily_dd, losses, initial):
    a, d = np.asarray(capital), np.asarray(dd)
    return {"median_end_capital": float(np.median(a)), "mean_end_capital": float(np.mean(a)),
        "end_capital_quantiles": quantiles(a), "net_profit_quantiles": quantiles(a - initial),
        "return_pct_quantiles": quantiles(100 * (a / initial - 1)),
        "drawdown_quantiles": quantiles(d), "daily_drawdown_quantiles": quantiles(daily_dd),
        "probabilities": {"negative_total_performance": float(np.mean(a < initial)),
            **{"drawdown_gt_%d_pct" % p: float(np.mean(d > p / 100)) for p in (5, 10, 20, 30)},
            **{"gain_ge_%d_pct" % p: float(np.mean(a >= initial * (1 + p / 100))) for p in (25, 50, 100)},
            "capital_doubling": float(np.mean(a >= 2 * initial)), "capital_depletion": float(np.mean(a <= 0))},
        "sequence_risk": {"worst_simulated_loss_streak": int(max(losses)),
            "median_max_loss_streak": float(np.median(losses)), "p95_max_loss_streak": float(np.percentile(losses, 95))}}


def true_trade_sample_status(count):
    if count<30:
        return "VERY_LOW_SAMPLE"
    if count<100:
        return "LOW_SAMPLE"
    return "REGULAR_EXPLORATORY"


def monte_carlo(pool, timeline, config, costs, risk, method, technical_test=False):
    if method not in ("TRADE_IID", "SESSION_BLOCK"):
        raise ValueError("unknown bootstrap method")
    base = {"method": method, "seed": config["seed"], "requested_paths": config["paths"],
        "measurement":"TECHNICAL_FUNCTION_TEST" if technical_test else "TRUE_TRADE_PORTFOLIO",
        "sample_status":true_trade_sample_status(len(pool)),
        "historical_trade_pool": len(pool), "block_sessions": config["block_sessions"] if method == "SESSION_BLOCK" else None,
        "conditioning": "Observed admitted complete trades; conditional on observed trade opportunity distribution, not a new signal/price forecast",
        "drawdown_frequency": "Observed intraday path marks including initial equity; daily distribution supplied separately",
        "same_time_streak_order": "Deterministic admission/ranking order for simultaneous exits; not statistically independent trades"}
    if not pool:
        return dict(base, status="INSUFFICIENT_TRADE_DATA", completed_paths=0)
    if len(pool)<30 and not technical_test:
        return dict(base,status="VERY_LOW_SAMPLE",completed_paths=0,
                    reason="Fewer than 30 true trades; technical validation only, no strategy Monte Carlo")
    n = len(timeline.days)
    if method == "SESSION_BLOCK" and n < 2 * config["block_sessions"]:
        return dict(base, status="UNAVAILABLE_FEWER_THAN_TWO_TIME_BLOCKS", completed_paths=0)
    if type(config["paths"]) is not int or config["paths"] < 1:
        raise ValueError("paths must be positive")
    rng = random.Random(config["seed"])
    capitals, depths, daily_depths, losses, counts, rejected, opens = [], [], [], [], [], [], []
    schedule = sorted(p["entry_t"] for p in pool)
    for path in range(config["paths"]):
        if method == "TRADE_IID":
            selected = [shift_trade(pool[rng.randrange(len(pool))], t, i) for i, t in enumerate(schedule)]
        else:
            selected = block_sample(pool, timeline, bootstrap_indices(rng, n, config["block_sessions"]))
        book = simulate(selected, timeline, config, costs, risk)
        capitals.append(book["end_equity"])
        depths.append(drawdowns([r["equity"] for r in book["curve"]])["maximum"])
        daily_depths.append(drawdowns([book["initial_equity"]] + [r["equity"] for r in book["daily_equity"]])["maximum"])
        losses.append(streaks([r["pnl"] for r in book["trades"]])[1])
        counts.append(len(book["trades"]))
        rejected.append(len(book["rejections"]))
        opens.append(book["open_positions"])
    return dict(base, status="AVAILABLE", completed_paths=config["paths"],
        flags=[true_trade_sample_status(len(pool))] if len(pool) < 100 else [],
        resampling_note="IID moves complete paths to observed entry times. Blocks retain whole calendar-session clusters and inactivity; dependence is broken at block joins. FX paths travel with trades and are not an independent currency model.",
        completed_trade_quantiles=quantiles(counts), rejection_count_quantiles=quantiles(rejected),
        paths_with_open_marked_positions=sum(v > 0 for v in opens),
        **summarize(capitals, depths, daily_depths, losses, config["initial_equity"]))
