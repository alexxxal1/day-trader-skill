"""Chronological, unlevered book for observed trade paths and their resamples."""
import bisect
import datetime as dt
import heapq
import math

from engine import friction
from portfolio_model import RISK, position_size, validate


def simulate(templates, timeline, config, costs, risk, detail=True):
    rules = validate(config)
    initial = float(config["initial_equity"])
    cash, active, closed, rejected = initial, {}, [], []
    queue, sequence = [], 0

    def push(t, kind, i, payload=None):
        nonlocal sequence
        sequence += 1
        heapq.heappush(queue, (t, kind, sequence, i, payload))

    ranked = sorted(templates, key=lambda p: (p["entry_t"], -p.get("trigger_setup_status", 0),
        -(p.get("RS_PROXY") or 0), -(p.get("liquidity") or 0), p["symbol"], p["id"]))
    for i, p in enumerate(ranked):
        if 0 <= p["entry_t"] < timeline.total:
            push(p["entry_t"], 3, i)
    for i, t in enumerate(timeline.ends):
        push(t, 2, i)
    daily, curve = [], [{"t": 0, "equity": initial}]
    previous, pos_integral, exposure_integral, max_positions = 0, 0, 0, 0
    daily_risk, daily_count = {}, {}
    flags = {"MODELLED_COSTS_NOT_OBSERVED_HISTORICAL_COSTS", "STOP_GAP_AND_FX_RISK_NOT_CAPPED"}
    accepted, admissions = [], []

    def equity():
        return cash + sum(p["qty"] * p["mark"] / p["fx"] if p["side"] == "long" else
            p["collateral"] + p["qty"] * (p["entry"] - p["mark"]) / p["fx"] for p in active.values())

    while queue:
        t, kind, _, i, payload = heapq.heappop(queue)
        eq = equity()
        elapsed = max(0, t - previous)
        pos_integral += elapsed * len(active)
        exposure_integral += elapsed * (sum(p["qty"] * p["mark"] / p["fx"] for p in active.values()) / eq if eq > 0 else 0)
        previous = t
        if kind == 0:
            if i in active:
                active[i]["mark"], active[i]["fx"] = payload
        elif kind == 1:
            if i not in active:
                continue
            p = active.pop(i)
            sign = 1 if p["side"] == "long" else -1
            fill = p["exit_mid"] - sign * friction(p["exit_mid"], costs)
            fee = p["qty"] * costs["commission_per_unit_roundtrip"] / 2 / p["exit_fx"]
            borrow = p["qty"] * p.get("borrow_cost_usd_roundtrip", 0) / p["exit_fx"]
            if sign == 1:
                proceeds = p["qty"] * fill / p["exit_fx"] - fee
                cash += proceeds
                pnl = proceeds - p["entry_cash"]
            else:
                pnl = p["qty"] * (p["entry"] - fill) / p["exit_fx"] - fee - p["entry_fee"] - borrow
                cash += p["collateral"] + pnl + p["entry_fee"]
            row = {"id": p["id"], "symbol": p["symbol"], "side": p["side"], "qty": p["qty"],
                "entry_t": p["entry_t"], "exit_t": t, "pnl": pnl, "r_multiple": pnl / p["planned_risk"],
                "planned_risk": p["planned_risk"], "entry": p["entry"], "exit": fill,
                "entry_fx": p["entry_fx"], "exit_fx": p["exit_fx"],
                "holding_minutes": t - p["entry_t"], "holding_sessions":
                bisect.bisect_left(timeline.ends, t) - bisect.bisect_right(timeline.starts, p["entry_t"]) + 2,
                "costs": p["entry_fee"] + fee + borrow + p["qty"] * (friction(p["entry_mid"], costs) / p["entry_fx"] + friction(p["exit_mid"], costs) / p["exit_fx"]),
                "exit_reason": p.get("exit_reason", "EMPIRICAL_PATH_EXIT")}
            closed.append(row)
        elif kind == 2:
            daily.append({"day": timeline.days[i], "equity": equity(), "positions": len(active)})
        else:
            source = ranked[i]
            day_index = bisect.bisect_right(timeline.starts, t) - 1
            when = timeline.opens[day_index] + dt.timedelta(minutes=t - timeline.starts[day_index])
            reason = None
            if any(p["symbol"] == source["symbol"] for p in active.values()):
                reason = "DUPLICATE_TICKER"
            elif len(active) >= config["max_positions"]:
                reason = "PORTFOLIO_SLOTS"
            elif source["symbol"] in rules["blocked"]:
                reason = "BLOCKED_TICKER"
            elif source["side"] == "short" and not (rules["allow_short"] and rules["allow_margin"]):
                reason = "SHORT_NOT_AUTHORIZED"
            elif source["side"] == "short" and (not source.get("borrow_verified", False) or
                    type(source.get("borrow_cost_usd_roundtrip")) not in (int, float) or
                    not math.isfinite(source["borrow_cost_usd_roundtrip"]) or source["borrow_cost_usd_roundtrip"] < 0):
                reason = "HISTORICAL_BORROW_UNAVAILABLE"
            elif daily_count.get(day_index, 0) >= min(rules["max_trades_per_day"], rules["max_orders_per_day"]):
                reason = "DAILY_TRADE_LIMIT"
            if not reason:
                reasons, _ = RISK.window_check(RISK.to_eastern(when), rules)
                if reasons:
                    reason = "ENTRY_WINDOW"
            try:
                sizing_costs = costs
                if source["side"] == "short" and not reason:
                    sizing_costs = dict(costs, commission_per_unit_roundtrip=
                        costs["commission_per_unit_roundtrip"] + source["borrow_cost_usd_roundtrip"])
                qty, unit = position_size(eq, cash, sum(p["planned_risk"] for p in active.values()),
                    source["entry_mid"], source["stop"], source["target"], source["side"], source["entry_fx"], sizing_costs, config, risk)
                unit["fee"] = costs["commission_per_unit_roundtrip"] / 2 / source["entry_fx"]
            except ValueError as exc:
                qty, unit = 0, None
                reason = reason or str(exc)
            if unit:
                # Existing absolute USD guards are evaluated in USD; only the
                # requested per-trade risk ceiling is replaced by equity*risk.
                remaining_daily = max(0, rules["max_daily_loss_usd"] - daily_risk.get(day_index, 0))
                qty = min(qty, int(remaining_daily / (unit["risk"] * source["entry_fx"]) + 1e-10))
                if unit["reward_risk"] < rules["min_reward_risk"]:
                    reason = reason or "NET_REWARD_RISK"
                if qty * source["entry_mid"] < rules["min_order_usd"]:
                    reason = reason or "MIN_ORDER"
            if qty <= 0:
                reason = reason or "CAPITAL_OR_RISK_LIMIT"
            if reason:
                rejected.append({"id": source["id"], "reason": reason})
                continue
            p = dict(source, qty=qty, entry=unit["entry"], entry_fee=qty * unit["fee"],
                     planned_risk=qty * unit["risk"], mark=source["entry_mid"], fx=source["entry_fx"])
            p["entry_cash"] = qty * unit["entry"] / p["entry_fx"] + p["entry_fee"]
            p["collateral"] = qty * unit["cash"] if p["side"] == "short" else 0
            cash -= p["entry_cash"] if p["side"] == "long" else p["collateral"] + p["entry_fee"]
            active[i] = p
            admissions.append({"id":p["id"],"t":t,"equity":equity(),"cash":cash,
                "used_risk":sum(a["planned_risk"] for a in active.values()),"positions":len(active)})
            daily_risk[day_index] = daily_risk.get(day_index, 0) + p["planned_risk"] * p["entry_fx"]
            daily_count[day_index] = daily_count.get(day_index, 0) + 1
            max_positions = max(max_positions, len(active))
            accepted.append(source)
            for mt, mid, rate in source["marks"]:
                if t <= mt <= min(source["exit_t"], timeline.total):
                    if mt == source["exit_t"]:
                        mid = source["exit_mid"]
                    push(mt, 0, i, (mid, rate))
            if source["exit_t"] <= timeline.total:
                push(source["exit_t"], 1, i)
        # Simultaneous marks are one portfolio valuation. Intermediate ticker
        # update order must not create a fictitious equity peak/drawdown.
        if kind != 0 or not queue or queue[0][0] != t or queue[0][1] != 0:
            curve.append({"t": t, "equity": equity()})
    if active:
        flags.add("EMPIRICAL_PATH_OPEN_AT_HORIZON_MARKED_NOT_LIQUIDATED")
    return {"initial_equity": initial, "end_equity": equity(), "trades": closed, "daily_equity": daily,
        "curve": curve, "rejections": rejected, "accepted_templates": accepted, "open_positions": len(active),
        "average_positions": pos_integral / timeline.total if timeline.total else 0,
        "max_positions": max_positions, "exposure": exposure_integral / timeline.total if timeline.total else 0,
        "flags": sorted(flags), "risk_per_trade": risk, "costs": costs, "admissions":admissions}
