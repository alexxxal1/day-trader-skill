"""Risk-sized empirical paths. Reuses existing timeline and bootstrap primitives."""
import bisect
from collections import Counter
import heapq
import math
import random

from common import write
from engine import cost_scenarios, friction
from portfolio_metrics import drawdowns, streaks
from portfolio_monte_carlo import block_sample, bootstrap_indices, shift_trade, summarize
from portfolio_sources import Timeline
from true_trade_execution import sample_status
from true_trade_replay import STRATEGIES


def template(trade, timeline):
    return {"id": trade["id"], "symbol": trade["ticker"], "side": trade["side"],
        "entry_t": timeline.at(trade["entry_time"]), "exit_t": timeline.at(trade["exit_time"]),
        "entry_mid": trade["entry_mid"], "exit_mid": trade["exit_mid"],
        "stop": trade["stop_initial"], "target": trade["target_initial"],
        "marks": [[timeline.at(t), mid, 1.0] for t, mid in trade["marks"]],
        "RS_PROXY": trade.get("RS_PROXY"), "liquidity": trade.get("liquidity", 0),
        "borrow_verified": trade.get("borrow_verified", False),
        "borrow_cost": trade.get("borrow_cost_usd_roundtrip")}


def simulate(pool, timeline, config, costs, risk):
    """Base units=floor(equity*risk/actual stop distance), capped by the book.

    Costs reduce cash/equity; shorts require explicit borrow data and 150%
    collateral. Mark prices are observed path closes, never future admission
    information. Portfolio constraints are admission constraints; gap losses
    can exceed initial stop risk and are reported, not clipped.
    """
    if not 0 < risk <= config["max_portfolio_risk"] <= 0.03 or config["leverage"] != 1 or not 1 <= config["max_positions"] <= 5:
        raise ValueError("unlevered risk/slot limits required")
    cash = float(config["initial_equity"])
    active, queue, closed, rejected, admissions = {}, [], [], [], []
    challenge = []
    curve = [{"t": 0, "equity": cash}]
    sequence = 0

    def push(t, kind, key, payload=None):
        nonlocal sequence
        sequence += 1
        heapq.heappush(queue, (t, kind, sequence, key, payload))

    def equity():
        return cash + sum(p["qty"] * p["mark"] if p["side"] == "long" else
            p["collateral"] + p["qty"] * (p["entry_fill"] - p["mark"]) for p in active.values())

    for i, p in enumerate(sorted(pool, key=lambda p: (p["entry_t"], -(p.get("RS_PROXY") or 0),
                                                     -(p.get("liquidity") or 0), p["symbol"], p["id"]))):
        if 0 <= p["entry_t"] < timeline.total:
            push(p["entry_t"], 3, i, p)
    for i, t in enumerate(timeline.starts):
        push(t, 2, i)
    maximum_exposure = 0.0
    while queue:
        t, kind, _, key, payload = heapq.heappop(queue)
        if kind == 0:
            if key in active:
                active[key]["mark"] = payload
        elif kind == 1:
            if key not in active:
                continue
            p = active.pop(key)
            sign = 1 if p["side"] == "long" else -1
            fill = p["exit_mid"] - sign * friction(p["exit_mid"], costs)
            fee = p["qty"] * costs["commission_per_unit_roundtrip"] / 2
            borrow = p["qty"] * (p.get("borrow_cost") or 0)
            pnl = sign * p["qty"] * (fill - p["entry_fill"]) - fee - p["entry_fee"] - borrow
            cash += p["qty"] * fill - fee if sign == 1 else p["collateral"] + sign * p["qty"] * (fill - p["entry_fill"]) - fee - borrow
            closed.append({"id": p["id"], "exit_t": t, "pnl": pnl, "qty": p["qty"],
                           "r_multiple": pnl / p["initial_stop_risk"], "holding_minutes": t - p["entry_t"]})
        elif kind == 2:
            pass  # explicit day-open valuation, including inactive sessions
        else:
            source = payload
            eq = equity()
            gross = sum(p["qty"] * p["mark"] for p in active.values())
            used = sum(p["initial_stop_risk"] for p in active.values())
            mid, stop = source["entry_mid"], source["stop"]
            distance = abs(mid - stop)
            reason = None
            if source["side"] == "short" and (not source.get("borrow_verified") or source.get("borrow_cost") is None):
                reason = "HISTORICAL_BORROW_UNAVAILABLE"
            elif len(active) >= config["max_positions"]:
                reason = "PORTFOLIO_SLOTS"
            elif any(p["symbol"] == source["symbol"] for p in active.values()):
                reason = "DUPLICATE_TICKER"
            if reason:
                rejected.append({"id": source["id"], "reason": reason})
                continue
            fee = costs["commission_per_unit_roundtrip"] / 2
            sign = 1 if source["side"] == "long" else -1
            fill = mid + sign * friction(mid, costs)
            entry_drag = friction(mid, costs) + fee
            reserve = fill + 2 * fee if sign == 1 else 1.5 * max(mid, stop) + 2 * fee
            if distance <= 0 or eq <= 0:
                qty = 0
            else:
                raw = eq * risk / distance
                caps = [raw, cash / reserve,
                    (eq - gross) / (mid + entry_drag),
                    (eq * config["max_portfolio_risk"] - used) / (distance + entry_drag * config["max_portfolio_risk"])]
                qty = max(0, math.floor(min(caps) + 1e-10))
            if qty <= 0:
                rejected.append({"id": source["id"], "reason": "CAPITAL_EXPOSURE_OR_RISK_LIMIT"})
                continue
            p = dict(source, qty=qty, mark=mid, entry_fill=fill, entry_fee=qty * fee,
                     initial_stop_risk=qty * distance, collateral=qty * 1.5 * max(mid, stop) if sign == -1 else 0)
            cash -= qty * fill + p["entry_fee"] if sign == 1 else p["collateral"] + p["entry_fee"]
            active[key] = p
            eq_after = equity()
            admissions.append({"t": t, "id": p["id"], "qty": qty, "equity": eq_after, "cash": cash,
                "positions": len(active), "stop_risk": sum(x["initial_stop_risk"] for x in active.values()),
                "entry_risk_fraction": p["initial_stop_risk"] / eq_after,
                "exposure": sum(x["qty"] * x["mark"] for x in active.values()) / eq_after})
            for mt, price, _ in source["marks"]:
                if t <= mt <= min(source["exit_t"], timeline.total):
                    push(mt, 0, key, source["exit_mid"] if mt == source["exit_t"] else price)
            if source["exit_t"] <= timeline.total:
                push(source["exit_t"], 1, key)
        # Atomic same-time marks prevent order-dependent fictitious drawdowns.
        if kind != 0 or not queue or queue[0][:2] != (t, 0):
            eq = equity()
            curve.append({"t": t, "equity": eq})
            day_index = key if kind == 2 else (bisect.bisect_right(timeline.starts, t) - 1 if kind == 3 else bisect.bisect_left(timeline.ends, t))
            day_index = min(len(timeline.days) - 1, max(0, day_index))
            challenge.append({"day": timeline.days[day_index], "equity": eq, "is_entry": kind == 3,
                              **({"risk_per_trade": admissions[-1]["entry_risk_fraction"]} if kind == 3 else {})})
            maximum_exposure = max(maximum_exposure, sum(p["qty"] * p["mark"] for p in active.values()) / eq if eq > 0 else 0)
    return {"initial_equity": config["initial_equity"], "end_equity": equity(), "curve": curve, "trades": closed,
        "admissions": admissions, "open_positions": len(active), "rejections": rejected,
        "challenge_observations": challenge,
        "max_observed_exposure": maximum_exposure,
        "drawdown_frequency": "Observed intraday closes plus fills; unobserved wicks are not a continuous equity curve"}


def monte_carlo(pool, timeline, config, costs, risk, method):
    if method not in ("TRADE_IID", "SESSION_BLOCK"):
        raise ValueError("unknown method")
    base = {"method": method, "sample_status": sample_status(len(pool)), "completed_paths": 0,
            "seed": config["seed"], "historical_trade_pool": len(pool)}
    if len(pool) < max(100, config["minimum_monte_carlo_trades"]):
        return dict(base, status="INSUFFICIENT_TRUE_TRADES")
    if any(p["side"] == "short" and not p.get("borrow_verified") for p in pool):
        return dict(base, status="HISTORICAL_BORROW_UNAVAILABLE")
    if config["paths"] < 10000:
        raise ValueError("at least 10000 paths required")
    if method == "SESSION_BLOCK" and len(timeline.days) < 2 * config["block_sessions"]:
        return dict(base, status="INSUFFICIENT_TIME_BLOCKS")
    rng = random.Random(config["seed"])
    schedule = sorted(p["entry_t"] for p in pool)
    capital, dd, losses, counts = [], [], [], []
    open_paths = 0
    for _ in range(config["paths"]):
        if method == "TRADE_IID":
            selected = [shift_trade(pool[rng.randrange(len(pool))], t, i) for i, t in enumerate(schedule)]
        else:
            selected = block_sample(pool, timeline, bootstrap_indices(rng, len(timeline.days), config["block_sessions"]))
        book = simulate(selected, timeline, config, costs, risk)
        capital.append(book["end_equity"])
        dd.append(drawdowns([r["equity"] for r in book["curve"]])["maximum"])
        losses.append(streaks([r["pnl"] for r in book["trades"]])[1])
        counts.append(len(book["trades"]))
        open_paths += bool(book["open_positions"])
    result = summarize(capital, dd, dd, losses, config["initial_equity"])
    # No daily-drawdown claim: only observed intraday mark paths were measured.
    result.pop("daily_drawdown_quantiles")
    return dict(base, status="AVAILABLE", completed_paths=config["paths"],
        paths_with_open_marked_positions=open_paths,
        conditioning="Empirical complete paths on observed entry schedule; session blocks include inactivity. No new price forecast.",
        **result)


def challenge_observations(book, timeline):
    """Adapter for the existing generic evaluate_challenge; no firm defaults."""
    return list(book["challenge_observations"])


def research(trades, market, config, start, end, output):
    timeline = Timeline(market, start, end)
    costs = cost_scenarios(config)["REALISTIC"]
    results = {}
    for strategy in STRATEGIES:
        pool = [template(t, timeline) for t in trades if t["strategy"] == strategy]
        status = sample_status(len(pool))
        runs = []
        for risk in config["risk_per_trade"]:
            book = simulate(pool, timeline, config, costs, risk)
            key = strategy + "-" + str(risk)
            write(output / (key + ".json"), book)
            mc = {m: monte_carlo(pool, timeline, config, costs, risk, m) for m in ("TRADE_IID", "SESSION_BLOCK")}
            write(output / (key + "-monte-carlo.json"), mc)
            runs.append({"risk": risk, "measurement": "TRUE_TRADE_PORTFOLIO" if len(pool) >= 100 else "TECHNICAL_FUNCTION_TEST",
                "sample_status": status, "admitted": len(book["trades"]), "end_capital": book["end_equity"],
                "rejections": dict(Counter(r["reason"] for r in book["rejections"])),
                "monte_carlo": mc})
        results[strategy] = {"true_trades": len(pool), "sample_status": status, "runs": runs}
    return results
