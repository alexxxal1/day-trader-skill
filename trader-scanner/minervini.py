"""Minervini Trend Template filter. Standard library, no order or entry signals.

Yahoo chart quote OHLC is already split adjusted (unlike adjclose, it is
not dividend adjusted). RAW input is supported only with explicit split events.
The 12-month proxy uses 252 close-to-close trading intervals, never SPY or
the symbol's own history as the cross-sectional ranking population.
"""

import datetime
import math

MIN_HISTORY = 260
YEAR = 252
CRITERIA = (
    "price_gt_sma150", "price_gt_sma200", "sma150_gt_sma200",
    "sma200_rising", "sma50_gt_sma150", "sma50_gt_sma200",
    "price_gt_sma50", "above_52w_low_ge_30pct", "below_52w_high_le_25pct",
    "RS_PROXY_ge_70",
)
NOTE = ("Trend Template PASS is a momentum / Stage-2 research candidate only. "
        "No automatic LONG signal. RS_PROXY is not the original proprietary "
        "IBD / Minervini RS rating.")


def positive(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def last_session(now, clock):
    """Latest completed NYSE session as of the pinned UTC clock."""
    local = clock.to_eastern(now)
    day = local.date()
    if clock.session(day) and local.time() >= clock.session(day)[1]:
        return day
    day -= datetime.timedelta(days=1)
    while not clock.session(day):
        day -= datetime.timedelta(days=1)
    return day


def daily_bars(series, now, clock, required=MIN_HISTORY):
    """Finite OHLC of completed sessions only, on a consistent split price basis.

    Undo later splits in saved Yahoo charts for historical clocks; for explicit
    RAW OHLC apply only splits effective by the clock. Never use quote metadata
    (current price, rolling extrema or current volume) as historical evidence.
    """
    cutoff = last_session(now, clock)
    meta = series.get("meta") or {}
    basis = series.get("price_basis")
    if meta.get("dataGranularity") != "1d":
        return [], "INSUFFICIENT_DATA: daily OHLC required"
    if basis not in ("YAHOO_SPLIT_ADJUSTED", "RAW"):
        return [], "INSUFFICIENT_DATA: unknown split adjustment basis"
    splits = []
    for event in (series.get("splits") or {}).values():
        try:
            ratio = float(event["numerator"]) / float(event["denominator"])
            day = clock.ts_to_eastern(event["date"]).date()
            if not positive(ratio):
                raise ValueError()
        except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
            return [], "INSUFFICIENT_DATA: invalid split event"
        splits.append((day, ratio))
    rows = {}
    invalid = set()
    for i, stamp in enumerate(series.get("t") or []):
        if not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            return [], "INSUFFICIENT_DATA: invalid timestamp"
        day = clock.ts_to_eastern(stamp).date()
        if day > cutoff or stamp > (now - datetime.datetime(1970, 1, 1)).total_seconds():
            continue
        if not clock.session(day):
            continue
        values = []
        for key in ("c", "h", "l"):
            col = series.get(key) or []
            values.append(col[i] if i < len(col) else None)
        close, high, low = values
        if not all(positive(v) for v in values) or not low <= close <= high:
            invalid.add(day)
            continue
        factor = 1.0
        for split_day, ratio in splits:
            if basis == "RAW" and day < split_day <= cutoff:
                factor /= ratio
            elif basis == "YAHOO_SPLIT_ADJUSTED" and split_day > cutoff:
                factor *= ratio
        vol = series.get("v") or []
        volume = vol[i] if i < len(vol) else None
        volume = volume / factor if positive(volume) else 0.0
        row = {"date": day, "close": close * factor, "high": high * factor,
               "low": low * factor, "volume": volume}
        if day in rows and rows[day] != row:
            invalid.add(day)
        rows[day] = row
    bars = sorted(rows.values(), key=lambda b: b["date"])
    # Do not silently compress a hole in the required indicator window.
    if bars and any(d >= bars[max(0, len(bars) - required)]["date"] for d in invalid):
        return [], "INSUFFICIENT_DATA: invalid or conflicting daily OHLC"
    return bars, None


def metrics(series, now, clock, trend_days=21):
    bars, error = daily_bars(series, now, clock, max(MIN_HISTORY, 200 + trend_days))
    if error:
        return {}, error
    if len(bars) < max(MIN_HISTORY, 200 + trend_days):
        return {}, "INSUFFICIENT_DATA: need at least %d daily bars" % max(MIN_HISTORY, 200 + trend_days)
    if bars[-1]["date"] != last_session(now, clock):
        return {}, "STALE_DATA: latest completed session missing"
    # A 252-interval return must represent about a year, not years of sparse data.
    if (bars[-1]["date"] - bars[-YEAR - 1]["date"]).days > 380:
        return {}, "INSUFFICIENT_DATA: sparse 12-month history"
    close = [b["close"] for b in bars]
    price = close[-1]
    low = min(b["low"] for b in bars[-YEAR:])
    high = max(b["high"] for b in bars[-YEAR:])
    sma200 = sum(close[-200:]) / 200
    previous = sum(close[-200 - trend_days:-trend_days]) / 200
    return {"price": price, "sma50": sum(close[-50:]) / 50,
            "sma150": sum(close[-150:]) / 150, "sma200": sma200,
            "sma200_previous": previous, "sma200_trend_pct": (sma200 / previous - 1) * 100,
            "trend_days": trend_days, "low_52w": low, "high_52w": high,
            "above_52w_low_pct": (price / low - 1) * 100,
            "below_52w_high_pct": (1 - price / high) * 100,
            "return_12m_pct": (price / close[-YEAR - 1] - 1) * 100,
            "dollar_volume_20d": sum(b["close"] * b["volume"] for b in bars[-20:]) / 20,
            "volume": bars[-1]["volume"], "history_bars": len(bars),
            "close_date": bars[-1]["date"].isoformat(),
            "price_basis": "SPLIT_ADJUSTED_OHLC"}, None


def percentile_ranks(returns):
    """0..100 by (average ordinal rank - 1)/(N - 1), ties share a rank.

    One symbol cannot form a relative-strength population. All-equal returns
    have rank 50. Keep full precision for the >= 70 threshold.
    """
    ordered = sorted((r, sym) for sym, r in returns.items() if math.isfinite(r))
    n = len(ordered)
    if n < 2:
        return {}
    out, i = {}, 0
    while i < n:
        j = i + 1
        while j < n and ordered[j][0] == ordered[i][0]:
            j += 1
        rank = 100 * ((i + j - 1) / 2) / (n - 1)
        for _, sym in ordered[i:j]:
            out[sym] = rank
        i = j
    return out


def evaluate(symbol, values, rank, error=None):
    checks = dict.fromkeys(CRITERIA)
    if values:
        p, m50, m150, m200 = (values[k] for k in ("price", "sma50", "sma150", "sma200"))
        # Multiplication avoids boundary errors from percent display rounding.
        checks.update(zip(CRITERIA, (
            p > m150, p > m200, m150 > m200, m200 > values["sma200_previous"],
            m50 > m150, m50 > m200, p > m50,
            p >= 1.30 * values["low_52w"], p >= 0.75 * values["high_52w"],
            rank >= 70 if rank is not None else None,
        )))
    passed = [k for k in CRITERIA if checks[k] is True]
    failed = [k for k in CRITERIA if checks[k] is not True]
    status = "PASS" if not failed and not error else "FAIL"
    fields = dict.fromkeys(("price", "sma50", "sma150", "sma200", "sma200_previous",
                           "sma200_trend_pct", "above_52w_low_pct", "below_52w_high_pct"))
    fields.update(values)
    return dict(fields, symbol=symbol, trend_template=status, RS_PROXY=rank,
                criteria=checks, fulfilled_criteria=passed, failed_criteria=failed,
                criteria_met=len(passed), reason=error or ("INSUFFICIENT_DATA: RS_PROXY population" if rank is None
                                                         else ("CRITERIA_NOT_MET" if failed else None)),
                analysis_only=True, signal=None)


def scan(data, universe, now, clock, trend_days=21, sort="rs"):
    if trend_days < 1:
        raise ValueError("trend_days must be positive")
    prepared, errors = {}, {}
    for sym in dict.fromkeys(universe):
        if sym not in data:
            prepared[sym], errors[sym] = {}, "INSUFFICIENT_DATA: no chart"
        else:
            prepared[sym], errors[sym] = metrics(data[sym], now, clock, trend_days)
    ranks = percentile_ranks({s: v["return_12m_pct"] for s, v in prepared.items() if v})
    rows = [evaluate(s, v, ranks.get(s), errors[s]) for s, v in prepared.items()]
    def key(row):
        rs = row["RS_PROXY"] if row["RS_PROXY"] is not None else -1
        liq = row.get("dollar_volume_20d", 0)
        strength = (liq, rs) if sort == "liquidity" else (rs, liq)
        return (row["trend_template"] != "PASS", -strength[0], -strength[1], row["symbol"])
    rows.sort(key=key)
    return rows, {"requested": len(prepared), "fetched": sum(s in data for s in prepared),
                  "eligible": sum(bool(v) for v in prepared.values()), "ranked": len(ranks),
                  "pass": sum(r["trend_template"] == "PASS" for r in rows),
                  "insufficient": sum((r["reason"] or "").startswith("INSUFFICIENT_DATA") for r in rows),
                  "stale": sum((r["reason"] or "").startswith("STALE_DATA") for r in rows)}
