"""Causal, one-session trade reconstruction; no network, no daily fallback."""
import datetime as dt
import math
import statistics as st
from decimal import Decimal

from common import iso, module, stamp
from engine import friction
from portfolio_metrics import streaks

EXITS = module("trader-exit", "exit")
RISK = module("trader-risk", "check")
EXIT_REASONS = ("STOP_EXIT", "TARGET_EXIT", "TIME_EXIT", "TRAILING_EXIT", "DATA_END", "INSUFFICIENT_EXIT_DATA")


def sample_status(n):
    return "VERY_LOW_SAMPLE" if n < 30 else "LOW_SAMPLE" if n < 100 else "EXPLORATORY_SAMPLE"


def opening(clock, day):
    date = dt.date.fromisoformat(day)
    return clock.eastern_to_utc(dt.datetime.combine(date, clock.session(date)[0]))


def cutoff(clock, day, config):
    date = dt.date.fromisoformat(day)
    scheduled = clock.eastern_to_utc(dt.datetime.combine(date, dt.time.fromisoformat(config["flat_by"])))
    # Existing trader-exit's five-minute early-close buffer.
    return min(scheduled, clock.close(day) - dt.timedelta(minutes=5))


def session_rows(market, symbol, day):
    """Choose frame from opening observations, never from a later outcome."""
    start = opening(market.clock, day)
    one = market.execution_bars(symbol, "1m", day)
    frame = "1m" if any(r["_start"] == start for r in one) else "5m"
    rows = market.execution_bars(symbol, frame, day)
    factor = market.basis_factor(symbol, start)
    rows = [dict(r, **{k: r[k] * factor for k in ("o", "h", "l", "c")}, v=r["v"] / factor)
            for r in rows if start <= r["_start"] and r["_end"] <= market.clock.close(day)]
    return frame, rows


def contiguous_prefix(rows, start):
    result, expected = [], start
    for r in rows:
        if r["_start"] != expected or r["_available"] > r["_end"]:
            break
        result.append(r)
        expected = r["_end"]
    return result


def native_bars(rows, clock):
    return [{"start": clock.to_eastern(r["_start"]), "end": r["_end"],
             "open": r["o"], "high": r["h"], "low": r["l"], "close": r["c"],
             "volume": r["v"], "minutes": int((r["_end"] - r["_start"]).total_seconds() / 60)} for r in rows]


def bar_exit(bar, stop, target, side, order="conservative"):
    if order not in ("conservative", "optimistic"):
        raise ValueError("unknown intrabar-order sensitivity")
    short = side == "short"
    stop_open = bar["o"] >= stop if short else bar["o"] <= stop
    target_open = bar["o"] <= target if short else bar["o"] >= target
    if stop_open:
        return bar["o"], "STOP_EXIT", [], True
    if target_open:
        return target, "TARGET_EXIT", [], True  # no favorable gap improvement
    stopped = bar["h"] >= stop if short else bar["l"] <= stop
    targeted = bar["l"] <= target if short else bar["h"] >= target
    flags = ["AMBIGUOUS_INTRABAR_ORDER"] if stopped and targeted else []
    if targeted and (not stopped or order == "optimistic"):
        return target, "TARGET_EXIT", flags, False
    if stopped:
        return stop, "STOP_EXIT", flags, False
    return None, None, [], False


def account_trade(entry_mid, exit_mid, stop, side, costs):
    """One share, USD; allowances are model assumptions, not historical quotes."""
    if side not in ("long", "short"):
        raise ValueError("invalid trade side")
    if any(type(x) not in (int, float) or not math.isfinite(x) or x <= 0 for x in (entry_mid, exit_mid, stop)):
        raise ValueError("positive finite prices required")
    if any(not math.isfinite(v) or v < 0 for v in costs.values()):
        raise ValueError("nonnegative finite costs required")
    sign = -1 if side == "short" else 1
    distance = sign * (entry_mid - stop)
    if distance <= 0:
        raise ValueError("stop on wrong side of actual open")
    spread = (entry_mid + exit_mid) * costs["spread_bps"] / 20000
    slippage = (entry_mid + exit_mid) * costs["slippage_bps_per_side"] / 10000
    fee = costs["commission_per_unit_roundtrip"]
    gross = sign * (exit_mid - entry_mid)
    net = gross - spread - slippage - fee
    # Same all-in initial risk convention as existing portfolio_model.unit_values.
    risk = distance + friction(entry_mid, costs) + friction(stop, costs) + fee
    return {"entry_price": entry_mid + sign * friction(entry_mid, costs),
            "exit_price": exit_mid - sign * friction(exit_mid, costs),
            "spread": spread, "slippage": slippage, "fees": fee, "costs": spread + slippage + fee,
            "gross_pnl": gross, "net_pnl": net, "stop_distance": distance,
            "initial_risk_including_modelled_costs": risk, "r_multiple": net / risk,
            "gross_r_multiple": gross / distance}


def reconstruct(plan, rows, clock, config, costs, order="conservative"):
    """A plan requires a native trigger and a stop already frozen at that clock.

    OHLC-touch times are intervals. Cash is released only at the bar end when
    an intrabar fill's exact time cannot be established. Never choose a low/high
    as entry. A missing next slot is rejected, not skipped for a better fill.
    """
    at = stamp(plan["trigger_time"])
    try:
        stop_known = stamp(plan["stop_known_at"])
    except (KeyError, ValueError, TypeError):
        return dict(plan, status="INSUFFICIENT_STOP_DATA", true_trade=False)
    if stop_known > at:
        return dict(plan, status="INSUFFICIENT_STOP_DATA", true_trade=False)
    if stamp(plan["setup_known_at"]) > at:
        return dict(plan, status="SETUP_NOT_AVAILABLE_AT_TRIGGER", true_trade=False)
    following = [r for r in rows if r["_start"] >= at]
    if not following or following[0]["_start"] != at:
        return dict(plan, status="NEXT_INTRADAY_BAR_MISSING", true_trade=False)
    first = following[0]
    if first["v"] <= 0:
        return dict(plan, status="INSUFFICIENT_EXECUTION_LIQUIDITY", true_trade=False)
    end = cutoff(clock, plan["session"], config)
    if at >= end:
        return dict(plan, status="AFTER_FLAT_BY", true_trade=False)
    stop = plan.get("stop_initial")
    if type(stop) not in (int, float) or not math.isfinite(stop) or stop <= 0:
        return dict(plan, status="INSUFFICIENT_STOP_DATA", true_trade=False)
    try:
        bracket = EXITS.compute(first["o"], "price", Decimal(str(stop)), "r", Decimal(str(config["target_r"])), side=plan["side"])
    except (EXITS.PlanError, ValueError, TypeError):
        return dict(plan, status="ENTRY_OUTSIDE_VALID_STOP", true_trade=False)
    stop, target = float(bracket["stop"]), float(bracket["target"])
    if "atr" in plan or "adr" in plan:
        risk = RISK.scanner_stop_context(first["o"], stop, plan["side"], plan.get("atr"), plan.get("adr"),
            cfg={"allow_short": True, "allow_margin": True, "paper": True})
        if risk["blocks"] or risk["warning"] or (plan["strategy"] in ("BREAKOUT", "CONFLUENCE_TRIGGERED") and risk["preferred"] is not True):
            return dict(plan, status="ENTRY_OPEN_NATIVE_RISK_BLOCK", true_trade=False)
    trade = dict(plan, entry_time=iso(at), entry_mid=first["o"], stop_initial=stop,
                 target_initial=target, intrabar_order=order, units=1,
                 ambiguity_flags=list(plan.get("ambiguity_flags", [])), true_trade=False)
    if plan["side"] == "short":
        trade["ambiguity_flags"].extend(["BORROW_DATA_MISSING", "SHORT_LOCATE_UNVERIFIED"])
    marks = [[iso(at), first["o"]]]
    expected = at
    for bar in following:
        if bar["_start"] != expected:
            trade.update(status="INSUFFICIENT_EXIT_DATA", exit_reason="INSUFFICIENT_EXIT_DATA", marks=marks)
            return trade
        if bar["v"] <= 0:
            trade.update(status="INSUFFICIENT_EXIT_DATA", exit_reason="INSUFFICIENT_EXIT_DATA", marks=marks)
            return trade
        if bar["_start"] == end:
            mid, reason, flags, at_open = bar["o"], "TIME_EXIT", [], True
        elif bar["_start"] > end or bar["_end"] > end:
            trade.update(status="INSUFFICIENT_EXIT_DATA", exit_reason="INSUFFICIENT_EXIT_DATA", marks=marks)
            return trade
        else:
            mid, reason, flags, at_open = bar_exit(bar, stop, target, plan["side"], order)
        if reason:
            when = bar["_start"] if at_open else bar["_end"]
            trade["ambiguity_flags"].extend(flags)
            if not at_open:
                trade["ambiguity_flags"].append("EXIT_TIME_BAR_RESOLUTION")
            marks.append([iso(when), mid])
            trade.update(account_trade(first["o"], mid, stop, plan["side"], costs))
            trade.update(exit_time=iso(when), exit_mid=mid, exit_reason=reason,
                exit_time_lower_bound=iso(bar["_start"]), exit_time_upper_bound=iso(when),
                holding_minutes=(when - at).total_seconds() / 60, holding_sessions=1,
                status="TRUE_TRADE", true_trade=True, marks=marks)
            return trade
        marks.append([iso(bar["_end"]), bar["c"]])
        expected = bar["_end"]
    trade.update(status="DATA_END", exit_reason="DATA_END", marks=marks)
    return trade


def trade_metrics(rows):
    rows = sorted((r for r in rows if r.get("true_trade")), key=lambda r: (r["exit_time"], r["id"]))
    pnl = [r["net_pnl"] for r in rows]
    winners, losers = [p for p in pnl if p > 0], [p for p in pnl if p < 0]
    rr = [r["r_multiple"] for r in rows]
    wins, losses = streaks(pnl)
    mean = lambda a: st.mean(a) if a else None
    return {"trades": len(rows), "sample_status": sample_status(len(rows)),
        "win_rate": len(winners) / len(rows) if rows else None,
        "average_winner": mean(winners), "average_loser": mean(losers),
        "median_trade": st.median(pnl) if pnl else None, "expectancy": mean(pnl),
        "profit_factor": sum(winners) / -sum(losers) if losers else None,
        "profit_factor_status": "DEFINED" if losers else "UNDEFINED_WITHOUT_LOSSES",
        "mean_r": mean(rr), "median_r": st.median(rr) if rr else None,
        "max_consecutive_losses": losses, "max_consecutive_wins": wins,
        "average_holding_minutes": mean([r["holding_minutes"] for r in rows]),
        "pnl_units": "USD per one share; no claim of observed historical transaction costs",
        "r_basis": "Net P&L / initial stop risk including modelled round-trip costs"}
