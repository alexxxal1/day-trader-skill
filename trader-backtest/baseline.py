"""Seeded random controls with matched dates/universe/costs and fixed rules."""
import bisect
import datetime as dt
import math
import random
import statistics as st

from common import stamp
from engine import friction, quantity, roundtrip_cost
from metrics import percentile


def summarize(values, actual, attempted):
    return {"attempted": attempted, "valid": len(values), "invalid": attempted - len(values),
            "median": st.median(values) if values else None,
            "p05": percentile(values, 0.05), "p95": percentile(values, 0.95),
            "fraction_better_than_strategy": sum(v > actual for v in values) / len(values) if values and actual is not None else None}


def signal_baseline(market, samples, config):
    """Mean forward-return control, separate from investable portfolios.

    Same number of observations, dates, horizons and side; without replacement
    within each observation timestamp. Invalid data draws are counted.
    """
    days = {d: i for i, d in enumerate(market.days)}
    prices = {s: {r["day"]: r for r in market.bars(s, "1d")} for s in market.universe}
    intra = {s: {r["end"]: r["c"] for r in (market.bars(s, "1m") or market.bars(s, "5m"))} for s in market.universe}
    out = {}
    for horizon in (1, 3, 5, 10, 20, 60):
        rows = [r for r in samples if r["horizon"] == horizon and r["return_pct"] is not None]
        if not rows:
            out[str(horizon)] = dict(summarize([], None, 0), flags=["NO_OBSERVATIONS"])
            continue
        grouped = {}
        for row in rows:
            grouped.setdefault(row["at"], []).append(row)
        choices = {}
        for at, group in grouped.items():
            row = group[0]
            day, end = row["day"], row["end_day"]
            for sym in market.universe:
                bars = prices[sym]
                required = market.days[days[day]:days[end] + 1]
                anchor = bars.get(day, {}).get("c") if stamp(at) == market.clock.close(day) else intra[sym].get(at)
                choices[at, sym] = ((bars[end]["c"] / anchor - 1) * 100 if anchor and all(d in bars for d in required) else None)
        values = []
        rng = random.Random(config["seed"] + horizon)
        attempts = config["randomizations"]
        for _ in range(attempts):
            selected = []
            for at, group in sorted(grouped.items()):
                if len(group) > len(market.universe):
                    break
                for row, sym in zip(group, rng.sample(market.universe, len(group))):
                    value = choices[at, sym]
                    if value is None:
                        selected = []
                        break
                    selected.append(-value if row["strategy"] == "PARABOLIC_SHORT" else value)
                if not selected:
                    break
            if len(selected) == len(rows):
                values.append(st.mean(selected))
        out[str(horizon)] = dict(summarize(values, st.mean(r["return_pct"] for r in rows), attempts),
                                 matched_observations=len(rows), flags=["SIGNAL_CONTROL_NOT_PORTFOLIO"])
    return out


def portfolio_baseline(market, simulation, config, costs, sizing, start, end):
    """Conditional random-ticker control on the accepted trade schedule.

    Match entries and realized holding duration, including side. Stop distance
    percentages transfer for risk sizing, but exits occur at the matched time;
    this is a duration-conditioned control, not a new random strategy with an
    independent exit rule. Failed counts/risk/capital/duplicate/data constraints
    invalidate an iteration, with no selection of a better replacement draw.
    """
    trades = simulation["trades"]
    if not trades:
        return dict(summarize([], None, 0), flags=["NO_EXECUTABLE_TRADES"])
    if any(t["entry_day"] != t["exit_day"] for t in trades):
        return dict(summarize([], None, 0), flags=["MULTI_SESSION_MATCHING_UNAVAILABLE"])
    days = market.timeline(start, end)
    # Intraday mid quotes at each matching timestamp, with no stale-price fill.
    quotes, close_index = {}, {}
    for sym in market.universe:
        rows = market.execution_bars(sym, "1m") or market.execution_bars(sym, "5m")
        quotes[sym] = ({r["start"]: r["o"] for r in rows}, {r["end"]: r["c"] for r in rows})
        close_index[sym] = ([r["_end"] for r in rows], [r["c"] for r in rows])
    schedule = sorted([(t["entry_at"], 1, i) for i, t in enumerate(trades)] +
                      [(t["exit_at"], 0, i) for i, t in enumerate(trades)])
    rng = random.Random(config["seed"])
    values, drawdowns, exposures = [], [], []
    baseline_flags = {"DURATION_CONDITIONED_RANDOM_CONTROL", "INVALID_DRAWS_REPORTED_NO_REPLACEMENT"}
    from metrics import max_drawdown
    total_minutes = sum((market.clock.close(d) - market.clock.eastern_to_utc(
        dt.datetime.combine(dt.date.fromisoformat(d), market.clock.session(dt.date.fromisoformat(d))[0]))).total_seconds() / 60 for d in days)
    for _ in range(config["randomizations"]):
        account = float(config["initial_equity"])
        active, realized = {}, {}
        valid, exposure_sum = True, 0.0
        assigned = {}
        for at, kind, i in schedule:
            trade = trades[i]
            sign = -1 if trade["side"] == "short" else 1
            if kind == 0:
                if i not in active:
                    valid = False
                    break
                p = active.pop(i)
                sym = assigned[i]
                mid = quotes[sym][1].get(at)
                if mid is None:
                    valid = False
                    break
                fill = mid - sign * friction(mid, costs)
                account += sign * (fill - p["entry"]) * p["qty"] - costs["commission_per_unit_roundtrip"] * p["qty"] / 2
                if sign == -1:
                    charges = [r for r in market.document.get("borrow", {}).get(sym, [])
                               if r["day"] == trade["entry_day"] and stamp(r["available_at"]) <= stamp(at)]
                    if charges:
                        charge = max(charges, key=lambda r: stamp(r["available_at"]))
                        account -= charge["cost_per_share"] * p["qty"]
                    else:
                        baseline_flags.add("BORROW_DATA_MISSING")
                realized[trade["exit_day"]] = account
                exposure_sum += p["entry"] * p["qty"] / p["equity"] * trade["holding_minutes"]
                continue
            pool = [s for s in market.universe if all(p["symbol"] != s for p in active.values())]
            if not pool:
                valid = False
                break
            sym = assigned[i] = rng.choice(pool)
            mid = quotes[sym][0].get(at)
            if mid is None or len(active) >= config["max_positions"] or any(p["symbol"] == sym for p in active.values()):
                valid = False
                break
            marks = {}
            for j, p in active.items():
                ts, ps = close_index[p["symbol"]]
                index = bisect.bisect_right(ts, stamp(at)) - 1
                if index < 0:
                    valid = False
                    break
                marks[j] = ps[index]
            if not valid:
                break
            eq = account + sum(p["sign"] * (marks[j] - p["entry"]) * p["qty"] for j, p in active.items())
            fill = mid + sign * friction(mid, costs)
            entry_mid = trade.get("entry_mid", trade["entry"])
            distance_pct = abs(entry_mid - trade["stop"]) / entry_mid
            stop = mid - sign * distance_pct * mid
            unit_risk = abs(mid - stop) + roundtrip_cost(mid, costs)
            used_gross = sum(abs(marks[j] * p["qty"]) for j, p in active.items())
            used_collateral = sum(abs(marks[j] * p["qty"]) * p["collateral_rate"] for j, p in active.items())
            used_risk = sum(p["risk"] for p in active.values())
            collateral_rate = 1.5 if sign == -1 else 1 / config["leverage"]
            qty = min(quantity(eq, mid, stop, costs, config, sizing),
                      max(0, math.floor((eq * config["leverage"] - used_gross) / (mid + roundtrip_cost(mid, costs)))),
                      max(0, math.floor((eq - used_collateral) / (mid * collateral_rate + roundtrip_cost(mid, costs)))),
                      max(0, math.floor((eq * config["max_portfolio_risk"] - used_risk) / unit_risk)))
            if qty <= 0:
                valid = False
                break
            account -= costs["commission_per_unit_roundtrip"] * qty / 2
            active[i] = {"symbol": sym, "qty": qty, "entry": fill, "sign": sign, "risk": qty * unit_risk,
                         "equity": eq, "collateral_rate": collateral_rate}
        if valid and not active:
            values.append((account / config["initial_equity"] - 1) * 100)
            curve, value = [config["initial_equity"]], config["initial_equity"]
            for day in days:
                value = realized.get(day, value)
                curve.append(value)
            drawdowns.append(max_drawdown(curve))
            exposures.append(exposure_sum / total_minutes if total_minutes else 0)
    actual = (simulation["daily_equity"][-1]["equity"] / config["initial_equity"] - 1) * 100
    return dict(summarize(values, actual, config["randomizations"]), matched_trades=len(trades),
                median_drawdown=st.median(drawdowns) if drawdowns else None,
                median_exposure=st.median(exposures) if exposures else None,
                exposure_method="Approximate: entry gross notional/equity weighted by matched holding minutes",
                flags=sorted(baseline_flags))
