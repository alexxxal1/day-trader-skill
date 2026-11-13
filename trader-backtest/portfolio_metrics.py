"""Account-currency metrics; undefined values remain null, never infinite JSON."""
import datetime as dt
import math
import statistics as st


def streaks(values):
    wins = losses = max_wins = max_losses = 0
    for x in values:
        wins = wins + 1 if x > 0 else 0
        losses = losses + 1 if x < 0 else 0
        max_wins, max_losses = max(max_wins, wins), max(max_losses, losses)
    return max_wins, max_losses


def drawdowns(values):
    if not values:
        return {"maximum": None, "average": None, "longest_observations": 0}
    peak, underwater, longest, depths = values[0], 0, 0, []
    for v in values:
        peak = max(peak, v)
        depth = max(0, 1 - v / peak) if peak > 0 else 0
        depths.append(depth)
        underwater = underwater + 1 if depth > 1e-12 else 0
        longest = max(longest, underwater)
    return {"maximum": max(depths), "average": st.mean(depths), "longest_observations": longest}


def metrics(book, timeline):
    initial, final = book["initial_equity"], book["end_equity"]
    daily = [initial] + [r["equity"] for r in book["daily_equity"]]
    dd = drawdowns(daily)
    intra = drawdowns([r["equity"] for r in book["curve"]])
    ordered = sorted(enumerate(book["trades"]), key=lambda pair: (pair[1]["exit_t"], pair[0]))
    trades = [r for _, r in ordered]
    pnl, rr = [r["pnl"] for r in trades], [r["r_multiple"] for r in trades]
    winners, losers = [v for v in pnl if v > 0], [v for v in pnl if v < 0]
    max_wins, max_losses = streaks(pnl)
    returns = [b / a - 1 for a, b in zip(daily, daily[1:]) if a > 0]
    vol = st.stdev(returns) if len(returns) > 1 else 0
    downside = math.sqrt(st.mean(min(0, r) ** 2 for r in returns)) if returns else 0
    years = ((timeline.closes[-1] - timeline.opens[0]).total_seconds() / (365.2425 * 86400)) if timeline.days else 0
    mean = lambda xs: st.mean(xs) if xs else None
    return {"end_capital": final, "net_profit": final - initial, "total_return_pct": 100 * (final / initial - 1),
        "cagr": (final / initial) ** (1 / years) - 1 if years > 0 and final > 0 else None,
        "cagr_note": "Annualized over calendar elapsed time; short studies are not long-term forecasts",
        "max_drawdown": dd["maximum"], "average_drawdown": dd["average"],
        "longest_drawdown_sessions": dd["longest_observations"], "intraday_max_drawdown": intra["maximum"],
        "drawdown_note": "Daily-close primary; average includes zero-depth sessions; terminal unrecovered spell included. Intraday uses observed bar marks, not wicks.",
        "trades": len(trades), "win_rate": len(winners) / len(pnl) if pnl else None,
        "profit_factor": sum(winners) / -sum(losers) if losers else None,
        "profit_factor_note": None if losers else "Undefined without losing trades",
        "expectancy": mean(pnl), "average_winner": mean(winners), "average_loser": mean(losers),
        "median_trade": st.median(pnl) if pnl else None, "average_r": mean(rr), "median_r": st.median(rr) if rr else None,
        "max_consecutive_losses": max_losses, "max_consecutive_wins": max_wins,
        "average_holding_minutes": mean([r["holding_minutes"] for r in trades]),
        "average_holding_sessions": mean([r["holding_sessions"] for r in trades]),
        "average_positions": book["average_positions"], "max_positions": book["max_positions"], "exposure": book["exposure"],
        "sharpe": mean(returns) / vol * math.sqrt(252) if len(returns) >= 60 and vol else None,
        "sortino": mean(returns) / downside * math.sqrt(252) if len(returns) >= 60 and downside else None,
        "ratio_note": "252 daily observations/year; risk-free and MAR zero; minimum 60 returns; descriptive only",
        "return_over_drawdown": (final / initial - 1) / dd["maximum"] if dd["maximum"] else None}
