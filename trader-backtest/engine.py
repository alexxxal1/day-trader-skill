"""Event-driven, cash/gross constrained simulation. Never produces orders."""
import datetime as dt
import math

from common import iso, module, stamp

EXPOSURE = module("trader-risk", "exposure")


def cost_scenarios(config):
    c = config["costs"]
    base = {"spread_bps": c["spread_bps"], "slippage_bps_per_side": c["slippage_bps_per_side"],
            "commission_per_unit_roundtrip": c["commission_per_unit_roundtrip"]}
    return {"IDEAL": dict.fromkeys(base, 0), "REALISTIC": base,
            "STRESS": dict(base, spread_bps=base["spread_bps"] * c["stress_spread_multiplier"],
                           slippage_bps_per_side=base["slippage_bps_per_side"] * 2)}


def friction(price, costs):
    return price * (costs["spread_bps"] / 2 + costs["slippage_bps_per_side"]) / 10000


def roundtrip_cost(price, costs):
    # Reuse the existing risk module's round-trip cost convention, without
    # current allowances or any financing/borrow estimate.
    return float(EXPOSURE.terms({"symbol": "BACKTEST", "side": "buy",
        "spread": price * costs["spread_bps"] / 10000,
        "slippage": price * 2 * costs["slippage_bps_per_side"] / 10000,
        "commission_per_unit": costs["commission_per_unit_roundtrip"]})["cost"])


def quantity(equity, price, stop, costs, config, sizing):
    distance = abs(price - stop)
    cost = roundtrip_cost(price, costs)
    if price <= 0 or distance <= 0:
        return 0
    if sizing == "equal_weight":
        budget = equity / config["max_positions"]
        return max(0, math.floor(budget / (price + cost)))
    if sizing == "fixed_risk":
        return max(0, math.floor(equity * config["risk_per_trade"] / (distance + cost)))
    raise ValueError("unknown sizing")


def exit_price(bar, stop, target, side):
    """Gap stops at open; ambiguous stop+target candles resolve to stop."""
    short = side == "short"
    if (bar["o"] >= stop if short else bar["o"] <= stop):
        return bar["o"], "GAP_STOP"
    if (bar["h"] >= stop if short else bar["l"] <= stop):
        return stop, "STOP"
    if (bar["l"] <= target if short else bar["h"] >= target):
        return target, "TARGET"
    return None, None


def simulate(market, plans, config, costs, sizing, start=None, end=None):
    start, end = start or config["start"], end or config["end"]
    days = market.timeline(start, end)
    account = float(config["initial_equity"])
    active, closed, rejected, flags = [], [], [], set()
    daily, exposure_minutes, session_minutes = [], 0.0, 0.0
    pending = []
    for source in plans:
        if not start <= source["day"] <= end:
            continue
        plan = dict(source)
        at = stamp(plan["at"])
        if stamp(plan.get("stop_known_at", plan["at"])) > at:
            rejected.append({"signal": plan["id"], "reason": "STOP_FROM_FUTURE"})
            continue
        if plan.get("instrument_type", "stock") == "cfd":
            # A CFD cannot borrow a stock's chart identity. This engine records
            # the unsupported result explicitly, never invents net financing.
            flags.add("CFD_NET_PERFORMANCE_UNAVAILABLE")
            rejected.append({"signal": plan["id"], "reason": "CFD_REQUIRES_SEPARATE_HISTORICAL_CONTRACT_AND_COST_DATA"})
            continue
        frame = "1m" if market.bars(plan["symbol"], "1m", day=plan["day"]) else "5m"
        # Daily-only explicit plans are supported. No trigger is inferred from
        # a daily wick; adapters only emit intraday-confirmed trade plans.
        if not market.bars(plan["symbol"], frame, day=plan["day"]):
            frame = "1d"
        candidates = [r for r in market.execution_bars(plan["symbol"], frame) if r["_start"] >= at and r["day"] <= end]
        if not candidates:
            rejected.append({"signal": plan["id"], "reason": "NO_NEXT_BAR"})
            continue
        first = candidates[0]
        # Never carry an intraday order across a missing slot or overnight.
        if frame != "1d" and first["_start"] != at:
            rejected.append({"signal": plan["id"], "reason": "NEXT_INTRADAY_BAR_MISSING"})
            continue
        if frame != "1d" and market.clock.to_eastern(first["_start"]).time() >= dt.time.fromisoformat(config["flat_by"]):
            rejected.append({"signal": plan["id"], "reason": "AFTER_FLAT_BY"})
            continue
        plan.update(_fill=first["_start"], _frame=frame, _factor=market.basis_factor(plan["symbol"], at))
        pending.append(plan)
    pending.sort(key=lambda p: (p["_fill"], -p.get("confluence_level", 0),
                 -(p.get("RS_PROXY") or 0), -(p.get("liquidity") or 0), p["symbol"], p["id"]))

    def equity():
        return account + sum(p["sign"] * (p["mark"] - p["entry"]) * p["qty"] for p in active)

    def finish(p, mid, when, reason):
        nonlocal account
        fill = mid - p["sign"] * friction(mid, costs)
        fee = p["qty"] * costs["commission_per_unit_roundtrip"] / 2
        gross = p["sign"] * (fill - p["entry"]) * p["qty"]
        carry = 0.0
        if p["side"] == "short":
            # Only explicit historical, dated fee observations may be charged.
            observations = market.document.get("borrow", {}).get(p["symbol"], [])
            span = [d for d in days if p["entry_day"] <= d <= market.clock.to_eastern(when).date().isoformat()]
            known = {}
            for row in observations:
                if stamp(row["available_at"]) <= when:
                    old = known.get(row["day"])
                    if old is None or stamp(row["available_at"]) > stamp(old["available_at"]):
                        known[row["day"]] = row
            if any(d not in known for d in span):
                flags.add("BORROW_DATA_MISSING")
            else:
                carry = sum(float(known[d]["cost_per_share"]) * p["qty"] for d in span)
        account += gross - fee - carry
        net = gross - fee - p["entry_fee"] - carry
        closed.append({k: p[k] for k in ("id", "strategy", "symbol", "side", "qty", "entry", "entry_mid", "stop", "target", "entry_day", "entry_at", "planned_risk")})
        closed[-1].update(exit_at=iso(when), exit_day=market.clock.to_eastern(when).date().isoformat(),
                          exit=fill, exit_reason=reason, pnl=net,
                          return_pct=100 * net / (p["qty"] * p["entry"]),
                          r_multiple=net / p["planned_risk"],
                          holding_sessions=1 + days.index(market.clock.to_eastern(when).date().isoformat()) - days.index(p["entry_day"]),
                          holding_minutes=(when - stamp(p["entry_at"])).total_seconds() / 60,
                          costs=p["entry_fee"] + fee + carry + p["qty"] * (friction(p["entry_mid"], costs) + friction(mid, costs)))
        active.remove(p)

    for day in days:
        date = dt.date.fromisoformat(day)
        hours = market.clock.session(date)
        opening = market.clock.eastern_to_utc(dt.datetime.combine(date, hours[0]))
        closing = market.clock.close(day)
        cutoff = min(closing, market.clock.eastern_to_utc(dt.datetime.combine(date, dt.time.fromisoformat(config["flat_by"]))))
        session_minutes += (closing - opening).total_seconds() / 60
        day_plans = [p for p in pending if p["_fill"].date() == opening.date() and opening <= p["_fill"] < closing]
        pairs = {(p["symbol"], p["_frame"]) for p in day_plans} | {(p["symbol"], p["_frame"]) for p in active}
        rows = {(sym, frame): market.execution_bars(sym, frame, day=day) for sym, frame in pairs}
        times = {opening, closing, cutoff}
        for bars in rows.values():
            times.update(r["_start"] for r in bars)
            times.update(r["_end"] for r in bars)
        previous = opening
        for now in sorted(t for t in times if opening <= t <= closing):
            eq = equity()
            gross = sum(abs(p["mark"] * p["qty"]) for p in active)
            exposure_minutes += max(0, (now - previous).total_seconds() / 60) * (gross / eq if eq > 0 else 0)
            previous = now
            for p in list(active):
                bar = next((r for r in rows.get((p["symbol"], p["_frame"]), []) if r["_end"] == now), None)
                if bar and bar["_start"] >= stamp(p["entry_at"]):
                    b = {k: bar[k] * p["_factor"] for k in ("o", "h", "l", "c")}
                    p["mark"] = b["c"]
                    if p.get("forced_exit_at"):
                        px, reason = (b["c"], "MATCHED_HOLD") if now >= stamp(p["forced_exit_at"]) else (None, None)
                    else:
                        px, reason = exit_price(b, p["stop"], p["target"], p["side"])
                        expiry = days.index(day) - days.index(p["entry_day"]) + 1 >= config["max_hold_sessions"]
                        if px is None and expiry and (now >= cutoff or p["_frame"] == "1d"):
                            px, reason = b["c"], "TIME_EXIT"
                    if px is not None:
                        finish(p, px, now, reason)
                elif p["_frame"] != "1d" and max(opening, stamp(p["entry_at"])) < now <= closing:
                    expected = (now - stamp(p["entry_at"])).total_seconds() % (int(p["_frame"][:-1]) * 60) == 0
                    if expected:
                        flags.add("INCOMPLETE_POSITION_HISTORY")
            for plan in [p for p in day_plans if p["_fill"] == now]:
                reason = None
                if len(active) >= config["max_positions"]:
                    reason = "PORTFOLIO_SLOTS"
                elif any(p["symbol"] == plan["symbol"] for p in active):
                    reason = "DUPLICATE_TICKER"
                bar = next(r for r in rows[plan["symbol"], plan["_frame"]] if r["_start"] == now)
                mid = bar["o"] * plan["_factor"]
                sign = -1 if plan["side"] == "short" else 1
                fill = mid + sign * friction(mid, costs)
                if sign * (fill - plan["stop"]) <= 0 or sign * (plan["target"] - fill) <= 0:
                    reason = reason or "OPEN_OUTSIDE_BRACKET"
                eq = equity()
                qty = quantity(eq, mid, plan["stop"], costs, config, sizing)
                used_notional = sum(abs(p["mark"] * p["qty"]) for p in active)
                used_collateral = sum(abs(p["mark"] * p["qty"]) * p["collateral_rate"] for p in active)
                used_risk = sum(p["planned_risk"] for p in active)
                unit_risk = abs(mid - plan["stop"]) + roundtrip_cost(mid, costs)
                capacity = max(0, eq * config["leverage"] - used_notional)
                collateral_rate = 1.5 if plan["side"] == "short" else 1 / config["leverage"]
                collateral_capacity = max(0, eq - used_collateral)
                qty = min(qty, max(0, math.floor(capacity / (mid + roundtrip_cost(mid, costs)))),
                          max(0, math.floor(collateral_capacity / (mid * collateral_rate + roundtrip_cost(mid, costs)))),
                          max(0, math.floor((eq * config["max_portfolio_risk"] - used_risk) / unit_risk)))
                if qty <= 0:
                    reason = reason or "CAPITAL_OR_RISK_LIMIT"
                if reason:
                    rejected.append({"signal": plan["id"], "reason": reason})
                    continue
                fee = qty * costs["commission_per_unit_roundtrip"] / 2
                account -= fee
                p = dict(plan, qty=qty, sign=sign, entry=fill, entry_mid=mid, entry_fee=fee,
                         mark=mid, entry_day=day, entry_at=iso(now), planned_risk=qty * unit_risk,
                         collateral_rate=collateral_rate)
                active.append(p)
                if plan["side"] == "short":
                    flags.add("SHORT_AVAILABILITY_UNKNOWN")
        daily.append({"day": day, "equity": equity(), "positions": len(active)})
    if active:
        flags.add("OPEN_OR_UNVALUED_POSITIONS")
    if config["leverage"] > 1:
        flags.add("LEVERAGED_SCENARIO")
    return {"trades": closed, "daily_equity": daily, "initial_equity": config["initial_equity"],
            "exposure": exposure_minutes / session_minutes if session_minutes else None,
            "rejections": rejected, "flags": sorted(flags), "open_positions": len(active),
            "sizing": sizing, "costs": costs}
