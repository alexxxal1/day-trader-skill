"""Price-return, trade and marked-to-market portfolio statistics."""
import math
import random
import statistics as st

from common import stamp

HORIZONS = (1, 3, 5, 10, 20, 60)


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * q
    lo = int(position)
    return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (position - lo)


def distribution(values):
    return {"n": len(values), "mean": st.mean(values) if values else None,
            "median": st.median(values) if values else None,
            "win_count": sum(v > 0 for v in values), "loss_count": sum(v < 0 for v in values),
            "win_rate": sum(v > 0 for v in values) / len(values) if values else None,
            "loss_rate": sum(v < 0 for v in values) / len(values) if values else None,
            "stdev": st.stdev(values) if len(values) > 1 else None,
            "maximum": max(values) if values else None, "minimum": min(values) if values else None}


def max_drawdown(equity):
    if not equity:
        return None
    peak, drawdown = equity[0], 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            drawdown = max(drawdown, 1 - value / peak)
    return drawdown


def mean_ci(samples, horizon, seed, iterations):
    """Moving blocks of signal dates; overlapping outcomes are dependent.

    Intervals are exploratory. If fewer than two full blocks are observed,
    return no interval instead of an artificially precise IID interval.
    """
    groups = {}
    for row in samples:
        groups.setdefault(row["day"], []).append(row["return_pct"])
    grouped = [(sum(v), len(v)) for _, v in sorted(groups.items())]
    n = len(grouped)
    if n < max(2 * horizon, 5) or iterations < 1:
        return None
    blocks = [(sum(v[0] for v in grouped[i:i + horizon]), sum(v[1] for v in grouped[i:i + horizon]))
              for i in range(n - horizon + 1)]
    rng = random.Random(seed)
    estimates = []
    for _ in range(iterations):
        selected = [rng.choice(blocks) for _ in range(math.ceil(n / horizon))]
        estimates.append(sum(v[0] for v in selected) / sum(v[1] for v in selected))
    return [percentile(estimates, 0.025), percentile(estimates, 0.975)]


def forward_samples(market, signals, end=None):
    daily = {s: {r["day"]: r for r in market.bars(s, "1d")} for s in market.universe + ["SPY", "QQQ"]}
    index = {d: i for i, d in enumerate(market.days)}
    out = []
    for signal in signals:
        at = stamp(signal["at"])
        day = signal["day"]
        basis = market.basis_factor(signal["symbol"], at)
        price = signal["reference_price"] / basis
        sign = -1 if signal["side"] == "short" else 1
        for horizon in HORIZONS:
            pos = index[day] + horizon
            target = market.days[pos] if pos < len(market.days) else None
            row = {"signal_id": signal["id"], "strategy": signal["strategy"], "symbol": signal["symbol"],
                   "day": day, "at": signal["at"], "horizon": horizon, "end_day": target,
                   "return_pct": None, "excess_return_pct": None, "spy_return_pct": None,
                   "max_positive_move_pct": None, "max_negative_move_pct": None, "issue": None}
            required = market.days[index[day] + 1:pos + 1]
            bars = daily.get(signal["symbol"], {})
            if target is None or (end and target > end) or len(required) != horizon:
                row["issue"] = "RIGHT_CENSORED"
            elif any(d not in bars for d in required):
                row["issue"] = "MISSING_OR_DELISTED_HISTORY"
            else:
                row["return_pct"] = sign * (bars[target]["c"] / price - 1) * 100
                sample = [bars[d] for d in required]
                # Never use the signal day's pre-trigger high/low as an
                # excursion. Only subsequent sessions enter this statistic.
                moves = [sign * (b[k] / price - 1) * 100 for b in sample for k in ("h", "l")]
                row["max_positive_move_pct"] = max(0, max(moves))
                row["max_negative_move_pct"] = min(0, min(moves))
                spy = daily["SPY"]
                if day in spy and all(d in spy for d in required):
                    anchor = spy[day]["c"]
                    if at < market.clock.close(day):
                        intra = [r for r in (market.bars("SPY", "1m") or market.bars("SPY", "5m"))
                                 if r["_end"] == at]
                        anchor = intra[-1]["c"] if intra else None
                    if anchor:
                        row["spy_return_pct"] = (spy[target]["c"] / anchor - 1) * 100
                        row["excess_return_pct"] = row["return_pct"] - row["spy_return_pct"]
            out.append(row)
    return out


def forward_metrics(samples, config):
    out = {}
    for horizon in HORIZONS:
        all_rows = [r for r in samples if r["horizon"] == horizon]
        rows = [r for r in all_rows if r["return_pct"] is not None]
        result = distribution([r["return_pct"] for r in rows])
        excess = [r["excess_return_pct"] for r in rows if r["excess_return_pct"] is not None]
        flags = []
        if len(rows) < config["low_sample_threshold"]:
            flags.append("LOW_SAMPLE_CONFIDENCE")
        ci = mean_ci(rows, horizon, config["seed"], config["bootstrap_samples"])
        if ci is None:
            flags.append("INSUFFICIENT_INDEPENDENT_BLOCKS_FOR_CI")
        result.update(signals=len(all_rows), censored_or_missing=len(all_rows) - len(rows),
                      distinct_signal_days=len({r["day"] for r in rows}),
                      max_positive_move_pct=max((r["max_positive_move_pct"] for r in rows), default=None),
                      max_negative_move_pct=min((r["max_negative_move_pct"] for r in rows), default=None),
                      excess_return_pct=st.mean(excess) if excess else None,
                      spy_pairs=len(excess), win_rate_vs_spy=sum(v > 0 for v in excess) / len(excess) if excess else None,
                      mean_ci95=ci, flags=flags)
        out[str(horizon)] = result
    return out


def portfolio_metrics(simulation):
    initial = simulation["initial_equity"]
    curve = [initial] + [r["equity"] for r in simulation["daily_equity"]]
    returns = [b / a - 1 for a, b in zip(curve[:-1], curve[1:]) if a > 0]
    volatility = st.stdev(returns) if len(returns) > 1 else None
    downside = math.sqrt(sum(min(0, r) ** 2 for r in returns) / len(returns)) if returns else None
    valid = not set(simulation["flags"]) & {"OPEN_OR_UNVALUED_POSITIONS", "INCOMPLETE_POSITION_HISTORY"}
    sharpe = st.mean(returns) / volatility * math.sqrt(252) if len(returns) >= 60 and volatility else None
    sortino = st.mean(returns) / downside * math.sqrt(252) if len(returns) >= 60 and downside else None
    return {"sessions": len(returns), "return_pct": (curve[-1] / initial - 1) * 100 if valid else None,
            "max_drawdown": max_drawdown(curve) if valid else None,
            "annualized_volatility": volatility * math.sqrt(252) if volatility is not None and valid else None,
            "sharpe": sharpe if valid else None, "sortino": sortino if valid else None,
            "ratio_method": "Daily marked equity; 252 sessions/year; risk-free and minimum acceptable return = 0; minimum 60 observations",
            "drawdown_frequency": "daily closes, including initial capital", "exposure": simulation["exposure"],
            "flags": simulation["flags"]}


def trade_metrics(simulation, threshold=30):
    trades = sorted(simulation["trades"], key=lambda t: (t["exit_at"], t["id"]))
    pnl = [r["pnl"] for r in trades]
    winners, losers = [p for p in pnl if p > 0], [p for p in pnl if p < 0]
    streak, worst = 0, 0
    for value in pnl:
        streak = streak + 1 if value < 0 else 0
        worst = max(worst, streak)
    returns = distribution([r["return_pct"] for r in trades])
    rr = distribution([r["r_multiple"] for r in trades])
    return {"n": len(trades), "win_rate": len(winners) / len(pnl) if pnl else None,
            "average_winner_usd": st.mean(winners) if winners else None,
            "average_loser_usd": st.mean(losers) if losers else None,
            "expectancy_usd": st.mean(pnl) if pnl else None,
            "profit_factor": sum(winners) / -sum(losers) if losers else None,
            "profit_factor_note": "undefined without losses" if not losers else None,
            "median_return_pct": returns["median"], "average_return_pct": returns["mean"],
            "average_r": rr["mean"], "median_r": rr["median"], "max_consecutive_losses": worst,
            "average_holding_sessions": st.mean([r["holding_sessions"] for r in trades]) if trades else None,
            "average_holding_minutes": st.mean([r["holding_minutes"] for r in trades]) if trades else None,
            "flags": ["LOW_SAMPLE_CONFIDENCE"] if len(trades) < threshold else []}


def benchmark(market, symbol, start, end, costs):
    from engine import friction
    days = market.timeline(start, end)
    bars = {r["day"]: r for r in market.bars(symbol, "1d")}
    if not days or any(d not in bars for d in days):
        return {"available": False, "flags": ["BENCHMARK_HISTORY_MISSING"]}
    first, last = bars[days[0]], bars[days[-1]]
    buy = first["o"] + friction(first["o"], costs)
    fee = costs["commission_per_unit_roundtrip"]
    curve = [(bars[d]["c"] - fee / 2) / buy for d in days]
    curve[-1] = (last["c"] - friction(last["c"], costs) - fee) / buy
    sim = {"initial_equity": 1, "daily_equity": [{"day": d, "equity": p} for d, p in zip(days, curve)],
           "exposure": 1, "flags": []}
    return dict(portfolio_metrics(sim), available=True, method="First session open to last session close; price return, no dividends; fractional buy-and-hold")
