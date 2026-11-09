"""Independent Qullamaggie research filters with explicit, causal proxies.

All clocks are naive UTC internally. Intraday bars are interval START stamps;
only completed, contiguous regular-session OHLC can confirm a trigger. No
order, portfolio mutation, proprietary rating or Minervini combination.
"""

import datetime as dt
import importlib.util
import json
import math
import os

MODES = ("qullamaggie-breakout", "qullamaggie-ep", "qullamaggie-parabolic")
DEFAULTS = {
    "leader_percentile": 98.0, "prior_move_pct": 30.0,
    "base_min": 10, "base_max": 40, "impulse_window": 63,
    "contraction_max": 0.85, "ma_distance_pct": 10.0,
    "gap_pct": 10.0, "rvol_min": 2.0, "stop_warning_multiple": 1.5,
    "prior_ep_move_pct": 50.0, "large_cap": 10000000000.0,
    "small_cap": 2000000000.0, "large_extension_pct": 50.0,
    "small_extension_pct": 300.0, "unknown_extension_pct": 100.0,
    "parabolic_window": 20, "strong_day_pct": 3.0,
    "strong_days": 3, "long_drawdown_pct": 50.0,
    "collapse_days": 10, "catalyst_hours": 96.0,
    "opening_ranges": [1, 5, 60], "min_dollar_volume": 1000000.0,
}
CATALYSTS = ("earnings", "guidance", "government", "regulation", "fda", "biotech",
             "contract", "partnership", "sector", "macro", "political")
SKILLS = ("trader-news", "trader-social", "trader-quiver", "trader-sentiment",
          "trader-entry", "trader-exit", "trader-budget", "trader-risk")
EPOCH = dt.datetime(1970, 1, 1)


def local_module(folder, name):
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), folder, name + ".py")
    spec = importlib.util.spec_from_file_location("qullamaggie_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config(overrides=None):
    out = dict(DEFAULTS)
    if overrides is not None and not isinstance(overrides, dict):
        raise ValueError("Qullamaggie configuration must be an object")
    if set(overrides or {}) - set(DEFAULTS):
        raise ValueError("unknown Qullamaggie configuration key")
    out.update(overrides or {})
    for key, value in out.items():
        if key == "opening_ranges":
            if not isinstance(value, list) or not value or any(type(n) is not int or n not in (1, 5, 60) for n in value):
                raise ValueError("opening_ranges must contain 1, 5 or 60")
        elif not positive(value):
            raise ValueError(key + " must be finite and positive")
    for key in ("base_min", "base_max", "impulse_window", "parabolic_window", "strong_days", "collapse_days"):
        if type(out[key]) is not int:
            raise ValueError(key + " must be an integer")
    if not 2 <= out["base_min"] <= out["base_max"] <= 100 or out["impulse_window"] < 21:
        raise ValueError("invalid base/impulse windows")
    if out["leader_percentile"] > 100 or out["contraction_max"] >= 1 or out["long_drawdown_pct"] >= 100:
        raise ValueError("invalid percentile/contraction/drawdown")
    if out["stop_warning_multiple"] < 1 or out["small_cap"] >= out["large_cap"]:
        raise ValueError("invalid stop warning or market cap bounds")
    if not 5 <= out["parabolic_window"] <= 63 or not 1 <= out["collapse_days"] <= 20 or out["strong_days"] > 20:
        raise ValueError("parabolic/collapse windows exceed available short-term context")
    return out


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def timestamp(value):
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    # Evidence without a timezone cannot establish availability.
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)


def iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def available(item, now):
    published = timestamp(item.get("published_at"))
    observed = timestamp(item.get("available_at"))
    return max(published, observed) if published and observed and max(published, observed) <= now else None


def merge_evidence(documents):
    """Accept explicit causal evidence and native skill reports.

    Headlines, social posts and disclosure trades remain supporting leads.
    Only an explicitly verified, categorized causal record confirms an EP.
    Disclosure availability is never the transaction date. Date-only releases
    are unavailable for intraday confirmation until a precise time is supplied.
    """
    out = {"events": [], "metadata": []}
    for doc in documents:
        if not isinstance(doc, dict):
            raise ValueError("evidence must be a JSON object")
        json.dumps(doc, allow_nan=False)
        for key in ("items", "results", "records"):
            if key in doc and (not isinstance(doc[key], list) or any(not isinstance(r, dict) for r in doc[key])):
                raise ValueError("native evidence." + key + " must be an object array")
        for key in ("events", "metadata"):
            rows = doc.get(key, [])
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise ValueError("evidence." + key + " must be an object array")
            out[key].extend(rows)
        skill = doc.get("created_by")
        if "items" in doc:  # trader-news --json
            for item in doc["items"]:
                out["events"].append({"symbol": item.get("ticker"), "source_skill": "trader-news",
                    "published_at": item.get("time"), "available_at": doc.get("checked"),
                    "reference": item.get("link"), "summary": item.get("title"), "confirmed": False})
        if skill == "trader-social":
            for row in doc.get("results", []):
                out["events"].append({"symbol": row.get("ticker"), "source_skill": skill,
                    "published_at": row.get("original_timestamp"), "available_at": doc.get("checked_at"),
                    "reference": row.get("source_reference"), "summary": row.get("summary"), "confirmed": False})
        if skill == "trader-quiver":
            for row in doc.get("records", []):
                out["events"].append({"symbol": row.get("ticker"), "source_skill": skill,
                    "published_at": row.get("disclosure_timestamp"), "available_at": doc.get("checked_at"),
                    "reference": row.get("source_reference"), "summary": "disclosure research context", "confirmed": False})
    return out


def mean(values):
    return sum(values) / len(values) if values else None


def volatility(bars, n=20):
    sample = bars[-n:]
    adr = mean([b["high"] - b["low"] for b in sample])
    adr_pct = mean([100 * (b["high"] / b["low"] - 1) for b in sample])
    tr = [max(b["high"] - b["low"], abs(b["high"] - a["close"]), abs(b["low"] - a["close"]))
          for a, b in zip(bars[:-1], bars[1:])]
    return {"adr": adr, "adr_pct": adr_pct, "atr": mean(tr[-14:])}


def prepare(series, now, clock):
    parser = clock.minervini_module()  # reuse OHLC normalization only, no Trend Template criteria
    bars, error = parser.daily_bars(series, now, clock, required=150)
    if error:
        return [], error
    if len(bars) < 127:
        return [], "INSUFFICIENT_DATA: need 127 completed daily bars (6M rank)"
    if bars[-1]["date"] != parser.last_session(now, clock):
        return [], "INSUFFICIENT_DATA: stale daily history"
    expected = []
    day = bars[-127]["date"]
    while day <= bars[-1]["date"]:
        if clock.session(day):
            expected.append(day)
        day += dt.timedelta(days=1)
    if expected != [b["date"] for b in bars[-127:]]:
        return [], "INSUFFICIENT_DATA: daily history has missing sessions"
    raw = {}
    for i, stamp in enumerate(series.get("t") or []):
        day = clock.ts_to_eastern(stamp).date()
        opens, closes = series.get("o") or [], series.get("c") or []
        if i < len(opens) and i < len(closes) and positive(opens[i]) and positive(closes[i]):
            raw[day] = opens[i] / closes[i]
    # The shared parser rebases to the last completed session. If a split has
    # already taken effect this morning, bring yesterday's bars onto today's
    # known price basis before comparing them with current intraday prices.
    current_factor = 1.0
    for event in (series.get("splits") or {}).values():
        split_day = clock.ts_to_eastern(event["date"]).date()
        if bars[-1]["date"] < split_day <= clock.to_eastern(now).date():
            current_factor /= float(event["numerator"]) / float(event["denominator"])
    for bar in bars:
        for key in ("high", "low", "close"):
            bar[key] *= current_factor
        bar["volume"] /= current_factor
        ratio = raw.get(bar["date"])
        opening = ratio * bar["close"] if ratio else None
        bar["open"] = opening if opening and bar["low"] <= opening <= bar["high"] else None
    return bars, None


def intraday_bars(series, now, clock):
    """Current session only. Missing/invalid slots disable confirmation.

    Pre/post-market bars are retained separately as context, never OR inputs.
    """
    if not series:
        return [], [], "missing intraday data"
    minutes = {"1m": 1, "5m": 5}.get((series.get("meta") or {}).get("dataGranularity"))
    if not minutes or series.get("price_basis") not in ("YAHOO_SPLIT_ADJUSTED", "RAW"):
        return [], [], "intraday needs known-basis 1m/5m OHLC"
    day = clock.to_eastern(now).date()
    hours = clock.session(day)
    if not hours:
        return [], [], "outside trading day"
    opening, closing = [dt.datetime.combine(day, t) for t in hours]
    cutoff = min(clock.to_eastern(now), closing)
    rows, extended, invalid = {}, {}, False
    for i, stamp in enumerate(series.get("t") or []):
        if not finite(stamp):
            invalid = True
            continue
        utc = EPOCH + dt.timedelta(seconds=stamp)
        local = clock.to_eastern(utc)
        end = utc + dt.timedelta(minutes=minutes)
        if local.date() != day or end > now:
            continue
        vals = [(series.get(key) or [])[i] if i < len(series.get(key) or []) else None
                for key in ("o", "h", "l", "c", "v")]
        o, h, l, c, v = vals
        valid = all(positive(x) for x in (o, h, l, c)) and l <= min(o, c) <= max(o, c) <= h
        if not valid:
            if opening <= local < closing:
                invalid = True
            continue
        # Yahoo snapshots may contain future split normalization. Rebase prices
        # and volume using only explicitly supplied corporate actions.
        factor = 1.0
        try:
            for event in (series.get("splits") or {}).values():
                split = clock.ts_to_eastern(event["date"]).date()
                ratio = event["numerator"] / event["denominator"]
                if not positive(ratio):
                    raise ValueError()
                if series["price_basis"] == "YAHOO_SPLIT_ADJUSTED" and split > day:
                    factor *= ratio
                elif series["price_basis"] == "RAW" and local.date() < split <= day:
                    factor /= ratio
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return [], [], "invalid intraday split event"
        row = {"start": local, "end": end, "open": o * factor, "high": h * factor,
               "low": l * factor, "close": c * factor,
               "volume": v / factor if finite(v) and v >= 0 else None, "minutes": minutes}
        target = rows if opening <= local < closing else extended
        if local in target and target[local] != row:
            invalid = True
        target[local] = row
    bars = [rows[k] for k in sorted(rows)]
    want = max(0, int((cutoff - opening).total_seconds() // (minutes * 60)))
    if invalid or len(bars) != want or any(b["start"] != opening + dt.timedelta(minutes=i * minutes)
                                          for i, b in enumerate(bars)):
        return [], [extended[k] for k in sorted(extended)], "incomplete/invalid regular-session grid"
    return bars, [extended[k] for k in sorted(extended)], None


def five_minute(bars):
    if not bars or bars[0]["minutes"] == 5:
        return bars
    out = []
    for i in range(0, len(bars) - 4, 5):
        group = bars[i:i + 5]
        out.append(dict(group[0], end=group[-1]["end"], close=group[-1]["close"],
                        high=max(b["high"] for b in group), low=min(b["low"] for b in group),
                        volume=sum(b["volume"] for b in group) if all(b["volume"] is not None for b in group) else None,
                        minutes=5))
    return out


def triggers(bars, side, cfg, parabolic=False):
    """Conservative close confirmations; return all observed events in time order."""
    events = []
    if not bars:
        return events
    step = bars[0]["minutes"]
    short = side == "sell"
    for minutes in cfg["opening_ranges"]:
        if minutes % step:
            continue  # a 5m candle cannot resolve a 1m opening range
        count = minutes // step
        if len(bars) <= count:
            continue
        level = (min(b["low"] for b in bars[:count]) if short else max(b["high"] for b in bars[:count]))
        for i, bar in enumerate(bars[count:], count):
            crossed = bar["close"] < level if short else bar["close"] > level
            if crossed:
                events.append({"type": ("ORL_" if short else "ORH_") + str(minutes) + "m",
                               "at": iso(bar["end"]), "level": level, "price": bar["close"], "index": i})
                break
    if parabolic:
        grouped = five_minute(bars)
        if not short:
            # The prior multi-day collapse supplies the downside extension;
            # a long reversal does not need another low after today's open.
            for bar in grouped:
                if bar["close"] > bar["open"]:
                    events.append({"type": "FIRST_GREEN_5m", "at": iso(bar["end"]),
                                   "price": bar["close"], "level": bar["open"],
                                   "index": next(i for i, b in enumerate(bars) if b["end"] == bar["end"])})
                    break
        extreme = grouped[0]["high" if short else "low"] if grouped else None
        extension = False
        for bar in grouped[1:] if short else []:
            extension = extension or (bar["high"] > extreme if short else bar["low"] < extreme)
            if extension and (bar["close"] < bar["open"] if short else bar["close"] > bar["open"]):
                events.append({"type": "FIRST_RED_5m" if short else "FIRST_GREEN_5m", "at": iso(bar["end"]),
                               "price": bar["close"], "level": bar["open"],
                               "index": next(i for i, b in enumerate(bars) if b["end"] == bar["end"])})
                break
            extreme = max(extreme, bar["high"]) if short else min(extreme, bar["low"])
        if short:
            cumulative, volume, cracked, bounced = 0.0, 0.0, False, False
            for i, bar in enumerate(bars):
                if bar["volume"] is None:
                    break  # no invented VWAP
                cumulative += (bar["high"] + bar["low"] + bar["close"]) / 3 * bar["volume"]
                volume += bar["volume"]
                if not volume:
                    continue
                vwap = cumulative / volume
                # Separate bars establish crack, subsequent bounce, then failure.
                if bounced and bar["close"] < vwap and bar["close"] < bar["open"]:
                    events.append({"type": "VWAP_FAILURE", "at": iso(bar["end"]), "price": bar["close"],
                                   "level": vwap, "index": i})
                    break
                if cracked and bar["high"] >= 0.995 * vwap:
                    bounced = True
                if i and bar["close"] < min(b["low"] for b in bars[:min(i, 5 // step or 1)]) and bar["close"] < vwap:
                    cracked = True
    return sorted(events, key=lambda e: (e["at"], e["type"]))


def impulse(bars):
    """Ordered trough -> later peak, with the peak in the final five sessions."""
    if len(bars) < 2:
        return 0.0
    if max(b["high"] for b in bars[-5:]) < max(b["high"] for b in bars):
        return 0.0  # an old peak cannot relabel the tail of an overlong base
    best = 0.0
    for i in range(max(1, len(bars) - 5), len(bars)):
        best = max(best, (bars[i]["high"] / min(b["low"] for b in bars[:i]) - 1) * 100)
    return best


def base_metrics(bars, n, cfg):
    base = bars[-n:]
    prior = bars[max(0, len(bars) - n - cfg["impulse_window"]):len(bars) - n]
    first, last = base[:n // 2], base[n // 2:]
    width = lambda sample: max(b["high"] for b in sample) - min(b["low"] for b in sample)
    early, late = volatility(first), volatility(last)
    return {"base_duration": n, "base_start": base[0]["date"].isoformat(),
            "base_high": max(b["high"] for b in base), "base_low": min(b["low"] for b in base),
            "range_contraction": width(last) / width(first) if width(first) else 1.0,
            "higher_lows": min(b["low"] for b in last) > min(b["low"] for b in first),
            "atr_contraction": late["atr"] / early["atr"] if early["atr"] else None,
            "adr_contraction": late["adr"] / early["adr"] if early["adr"] else None,
            "prior_move_pct": impulse(prior), "impulse_bars": len(prior)}


def breakout(bars, ranks, cfg):
    # Latest completed session is the daily test bar; bases end before it.
    candidates = [base_metrics(bars[:-1], n, cfg) for n in range(cfg["base_min"], cfg["base_max"] + 1)
                  if len(bars) - n >= 22]
    def valid(m):
        return (m["prior_move_pct"] + 1e-10 >= cfg["prior_move_pct"] and m["impulse_bars"] >= 21
                and m["range_contraction"] <= cfg["contraction_max"]
                and m["adr_contraction"] is not None and m["adr_contraction"] <= cfg["contraction_max"])
    good = [m for m in candidates if valid(m)]
    metric = min(good or candidates, key=lambda m: (m["range_contraction"], -m["base_duration"]))
    close = bars[-1]["close"]
    for n in (10, 20, 50):
        sma = mean([b["close"] for b in bars[-n:]])
        old = mean([b["close"] for b in bars[-n - 5:-5]])
        metric["sma" + str(n)] = sma
        metric["distance_sma" + str(n) + "_pct"] = 100 * (close / sma - 1)
        metric["sma" + str(n) + "_rising"] = sma > old
    leaders = [k for k, value in ranks.items() if value is not None and value >= cfg["leader_percentile"]]
    metric.update(leader_periods=leaders, daily_breakout=close > metric["base_high"],
                  ma_support=all(abs(metric["distance_sma" + str(n) + "_pct"]) <= cfg["ma_distance_pct"]
                                 and metric["sma" + str(n) + "_rising"] for n in (10, 20)))
    reasons = []
    if not any(v is not None for v in ranks.values()):
        return "INSUFFICIENT_DATA", metric, ["cross-sectional ranks unavailable"], 0
    if not leaders:
        reasons.append("not in configured leader percentiles")
    if metric["prior_move_pct"] + 1e-10 < cfg["prior_move_pct"]:
        reasons.append("prior impulse too weak")
    if not good:
        reasons.append("no valid ordered 2-8 week contracting base")
    ready = bool(leaders and good)
    if close < metric["base_low"]:
        return "FAILED", metric, ["completed daily close lost base support"], 0
    quality = sum((bool(leaders), len(leaders) > 1, bool(good), metric["higher_lows"], metric["ma_support"])) * 20
    return ("SETUP_READY" if ready else "CANDIDATE" if leaders else "FAILED"), metric, reasons, quality


def evidence_for(evidence, symbol, now, event_day, clock, cfg):
    confirmed, support, rejected = [], [], 0
    for item in evidence.get("events", []):
        if item.get("symbol") != symbol or item.get("source_skill") not in SKILLS:
            continue
        when = available(item, now)
        published = timestamp(item.get("published_at"))
        event_end = clock.eastern_to_utc(dt.datetime.combine(event_day, dt.time(23, 59, 59)))
        if (when is None or published is None or published > event_end
                or (event_end - published).total_seconds() > cfg["catalyst_hours"] * 3600):
            rejected += 1
            continue
        clean = {k: item.get(k) for k in ("source_skill", "category", "reference", "summary", "earnings")}
        clean.update(available_at=iso(when), published_at=iso(published))
        support.append(clean)
        if (item.get("confirmed") is True and item.get("category") in CATALYSTS
                and item.get("reference") and not item.get("conflicting")):
            confirmed.append(clean)
    return confirmed, support, rejected


def ep(bars, intraday, extended, catalysts, cfg):
    active = bool(intraday)
    event = intraday[0] if active else bars[-1]
    prior = bars if active else bars[:-1]
    opening = event.get("open")
    baseline = prior[-20:]
    adv = mean([b["volume"] for b in baseline]) if all(b["volume"] > 0 for b in baseline) else None
    volume = (sum(b["volume"] for b in intraday) if all(b["volume"] is not None for b in intraday) else None) if active else event["volume"] or None
    ratio = volume / adv if volume is not None and adv else None
    early = [b for b in intraday if (b["start"].time() < dt.time(10, 0))]
    early_volume = sum(b["volume"] for b in early) if early and all(b["volume"] is not None for b in early) else None
    ext_volume = sum(b["volume"] for b in extended) if extended and all(b["volume"] is not None for b in extended) else None
    moves = {str(n) + "d": (prior[-1]["close"] / prior[-n - 1]["close"] - 1) * 100 for n in (63, 126) if len(prior) > n}
    earnings = [c["earnings"] for c in catalysts if isinstance(c.get("earnings"), dict)]
    positive_fields = set()
    supplied_fields = set()
    for values in earnings:
        for key in ("eps_growth_pct", "revenue_growth_pct", "analyst_beat_pct"):
            if finite(values.get(key)):
                supplied_fields.add(key)
                if values[key] > 0:
                    positive_fields.add(key)
        if isinstance(values.get("guidance"), str):
            supplied_fields.add("guidance")
            if values["guidance"].lower() in ("raised", "increased", "angehoben"):
                positive_fields.add("guidance")
    metric = {"gap_pct": (opening / prior[-1]["close"] - 1) * 100 if opening else None,
              "gap_open": opening, "previous_close": prior[-1]["close"], "adv20": adv,
              "volume": volume, "rvol": ratio, "volume_basis": "current regular session cumulative / prior ADV20" if active else "last completed session / prior ADV20",
              "first_30m_adv_ratio": early_volume / adv if early_volume is not None and adv else None,
              "extended_volume_adv_ratio": ext_volume / adv if ext_volume is not None and adv else None,
              "prior_move": moves, "already_extended": any(v >= cfg["prior_ep_move_pct"] for v in moves.values()),
              "earnings": earnings,
              "earnings_quality": {"supplied_fields": sorted(supplied_fields), "positive_fields": sorted(positive_fields),
                                   "points": 2.5 * len(positive_fields)},
              "event_date": (event["start"].date() if active else event["date"]).isoformat()}
    if not opening or ratio is None:
        return "INSUFFICIENT_DATA", metric, ["gap open or volume/ADV unavailable"], 0
    gap_ok = opening + 1e-10 >= prior[-1]["close"] * (1 + cfg["gap_pct"] / 100)
    if not gap_ok:
        return "FAILED", metric, ["gap below threshold"], 0
    # Cumulative early session volume >= 1x ADV is an explicit alternative to
    # full-session RVOL>=2; no daily-volume extrapolation is used.
    vol_ok = ratio >= cfg["rvol_min"] or (early_volume is not None and adv and early_volume >= adv)
    reasons = []
    if not vol_ok:
        reasons.append("elevated volume not confirmed yet")
    if not catalysts:
        reasons.append("no confirmed point-in-time catalyst; gap cause unknown")
    if metric["already_extended"]:
        reasons.append("strong previous multi-month move: quality penalty only")
    quality = 35 + 25 * bool(vol_ok) + 25 * bool(catalysts) + 5 * (not metric["already_extended"]) + 2.5 * len(positive_fields)
    return "SETUP_READY" if vol_ok and catalysts else "CANDIDATE", metric, reasons, quality


def extension(bars, cap, cfg):
    category = "LARGE" if cap is not None and cap >= cfg["large_cap"] else "SMALL" if cap is not None and cap < cfg["small_cap"] else "UNKNOWN_OR_MID"
    threshold = cfg["large_extension_pct"] if category == "LARGE" else cfg["small_extension_pct"] if category == "SMALL" else cfg["unknown_extension_pct"]
    sample = bars[-cfg["parabolic_window"] - 1:]
    best, trough = 0.0, sample[0]["close"]
    for bar in sample[1:]:
        best = max(best, (bar["close"] / trough - 1) * 100)
        trough = min(trough, bar["close"])
    # The move must finish near its peak; an old spike alone is not overextension.
    current = (sample[-1]["close"] / min(b["close"] for b in sample) - 1) * 100
    streak = 0
    for prev, bar in reversed(list(zip(bars[:-1], bars[1:]))):
        if bar["close"] + 1e-10 < prev["close"] * (1 + cfg["strong_day_pct"] / 100):
            break
        streak += 1
    c = [b["close"] for b in bars]
    recent = c[-1] / c[-6] - 1
    earlier = c[-6] / c[-11] - 1
    accelerating = recent > 0 and earlier > 0 and recent >= 1.5 * earlier
    return {"market_cap": cap, "cap_category": category, "extension_threshold_pct": threshold,
            "extension_pct": current, "maximum_ordered_move_pct": best,
            "strong_days": streak, "accelerating": accelerating,
            "overextended": current + 1e-10 >= threshold and (streak >= cfg["strong_days"] or accelerating)}


def parabolic(bars, cap, cfg, variant):
    metric = extension(bars, cap, cfg)
    if variant == "short":
        return ("SETUP_READY" if metric["overextended"] else "FAILED"), metric, ([] if metric["overextended"] else ["cap-aware extension/streak not met"]), 80 if metric["overextended"] else 0
    # Locate a recent peak, prove prior parabolic structure at that time, then
    # require a completed breakdown and collapse. No future peak selection.
    tail = min(cfg["collapse_days"], len(bars) - 12)
    peak_i = max(range(len(bars) - tail - 1, len(bars) - 1), key=lambda i: bars[i]["high"])
    peak = bars[peak_i]["high"]
    drawdown = (1 - bars[-1]["close"] / peak) * 100
    before = extension(bars[:peak_i + 1], cap, cfg)
    breakdown = any(bars[i]["close"] < bars[i - 1]["low"] for i in range(peak_i + 1, len(bars)))
    metric.update(drawdown_pct=drawdown, peak_date=bars[peak_i]["date"].isoformat(),
                  collapse_sessions=len(bars) - peak_i - 1, prior_parabolic=before["overextended"],
                  successful_breakdown=breakdown)
    ready = before["overextended"] and breakdown and bars[-1]["close"] <= peak * (1 - cfg["long_drawdown_pct"] / 100)
    return "SETUP_READY" if ready else "FAILED", metric, [] if ready else ["no proven parabolic breakdown and 50%+ collapse"], 75 if ready else 0


def latest_cap(evidence, symbol, now):
    values = [r for r in evidence.get("metadata", []) if r.get("symbol") == symbol
              and available(r, now) and r.get("reference") and positive(r.get("market_cap"))]
    return max(values, key=lambda r: available(r, now))["market_cap"] if values else None


def apply_trigger(row, events, bars, catalysts, cfg):
    """Freeze LoD/HoD at event time, then invalidate on subsequent bars."""
    if row["setup_status"] != "SETUP_READY" or not events:
        return
    short = row["variant"] == "PARABOLIC_SHORT"
    for event in events:
        if row["setup"] == "qullamaggie-ep":
            known = [c for c in catalysts if timestamp(c["available_at"]) <= timestamp(event["at"])]
            if not known:
                continue
            # Confirm volume at the trigger, never with later cumulative volume.
            prior_adv = row["metrics"]["adv20"]
            prefix = bars[:event["index"] + 1]
            if not all(b["volume"] is not None for b in prefix):
                continue
            volume = sum(b["volume"] for b in prefix)
            opening_30 = [b for b in prefix if b["start"].time() < dt.time(10, 0)]
            early_volume = sum(b["volume"] for b in opening_30)
            if volume < prior_adv * cfg["rvol_min"] and early_volume < prior_adv:
                continue
        prefix = bars[:event["index"] + 1]
        stop = event["level"] if event["type"] == "VWAP_FAILURE" else (max(b["high"] for b in prefix) if short else min(b["low"] for b in prefix))
        row.update(setup_status="TRIGGERED", trigger=dict(event, current=True),
                   stop={"price": stop, "basis": "VWAP reclaim" if event["type"] == "VWAP_FAILURE" else "HIGH_OF_DAY" if short else "LOW_OF_DAY",
                         "as_of": event["at"]})
        later = bars[event["index"] + 1:]
        if event["type"] == "VWAP_FAILURE":
            cumulative = sum((b["high"] + b["low"] + b["close"]) / 3 * (b["volume"] or 0) for b in prefix)
            volume = sum(b["volume"] or 0 for b in prefix)
            for bar in later:
                if bar["volume"] is None:
                    row["reasons"].append("later VWAP unavailable")
                    break
                cumulative += (bar["high"] + bar["low"] + bar["close"]) / 3 * bar["volume"]
                volume += bar["volume"]
                if volume and bar["close"] >= cumulative / volume:
                    row.update(setup_status="FAILED", invalidated_at=iso(bar["end"]))
                    row["reasons"].append("VWAP reclaimed after failure")
                    break
        elif any(b["high"] >= stop if short else b["low"] <= stop for b in later):
            row["setup_status"] = "FAILED"
            row["reasons"].append("initial stop breached after structural trigger")
        return


def scan(data, universe, now, clock, mode, intraday=None, evidence=None, overrides=None, risk_cfg=None, variant="both"):
    if mode not in MODES or variant not in ("short", "long", "both"):
        raise ValueError("invalid mode/variant")
    cfg = config(overrides)
    evidence = evidence or {}
    daily, errors = {}, {}
    for sym in dict.fromkeys(universe):
        daily[sym], errors[sym] = prepare(data[sym], now, clock) if sym in data else ([], "INSUFFICIENT_DATA: no daily chart")
    periods = {"1M": 21, "3M": 63, "6M": 126}
    ranks = {period: clock.minervini_module().percentile_ranks({sym: (bars[-1]["close"] / bars[-n - 1]["close"] - 1) * 100
             for sym, bars in daily.items() if bars}) for period, n in periods.items()}
    local = clock.to_eastern(now)
    hours = clock.session(local.date())
    active = bool(hours and hours[0] <= local.time() < hours[1])
    risk = local_module("trader-risk", "check")
    results = []
    for sym, bars in daily.items():
        variants = (["short", "long"] if variant == "both" else [variant]) if mode == MODES[2] else ["long"]
        source = (intraday or {}).get(sym, {})
        # Prefer 1m, whose bars also resolve 5m/60m ranges.
        series = (source.get("1m") or source.get("5m")) if isinstance(source, dict) and "t" not in source else source
        intra, extended, intra_error = intraday_bars(series, now, clock)
        for direction in variants:
            rank = {p: ranks[p].get(sym) for p in periods}
            row = {"symbol": sym, "setup": mode, "variant": "PARABOLIC_" + direction.upper() if mode == MODES[2] else "BREAKOUT" if mode == MODES[0] else "EP",
                   "setup_status": "INSUFFICIENT_DATA", "price": bars[-1]["close"] if bars else None,
                   "ranks": rank, "adr": None, "adr_pct": None, "atr": None, "volume": None,
                   "rvol": None, "catalyst": [], "trigger": None, "stop": None, "setup_quality": 0,
                   "action": "NO_SETUP", "signal": None, "trade_allowed": False, "analysis_only": True,
                   "paper": True, "metrics": {}, "reasons": [], "intraday_error": intra_error,
                   "session_context": "REGULAR_SESSION" if active else "PRE_MARKET_LAST_SESSION" if hours and local.time() < hours[0] else "LAST_SESSION",
                   "daily_as_of": bars[-1]["date"].isoformat() if bars else None}
            if not bars:
                row["reasons"].append(errors[sym])
                results.append(row)
                continue
            prior = bars[:-1] if mode == MODES[1] and not intra else bars
            row["performance_pct"] = {p: (bars[-1]["close"] / bars[-n - 1]["close"] - 1) * 100 for p, n in periods.items()}
            row.update(volatility(prior))
            adv = mean([b["volume"] for b in prior[-20:]])
            row.update(volume=bars[-1]["volume"], rvol=bars[-1]["volume"] / adv if adv else None)
            day = local.date() if intra else bars[-1]["date"]
            catalysts, support, rejected = evidence_for(evidence, sym, now, day, clock, cfg)
            row.update(catalyst=catalysts, supporting_evidence=support, unavailable_evidence=rejected)
            cap = latest_cap(evidence, sym, now)
            if mode == MODES[0]:
                status, metrics, reasons, quality = breakout(bars, rank, cfg)
            elif mode == MODES[1]:
                status, metrics, reasons, quality = ep(bars, intra, extended, catalysts, cfg)
                row.update(volume=metrics["volume"], rvol=metrics["rvol"])
            else:
                status, metrics, reasons, quality = parabolic(bars, cap, cfg, direction)
                row["mean_reversion_zones"] = {"sma10": mean([b["close"] for b in bars[-10:]]), "sma20": mean([b["close"] for b in bars[-20:]]), "guaranteed_target": False}
            row.update(setup_status=status, metrics=metrics, reasons=reasons, setup_quality=quality)
            row["trailing_context"] = {"sma10": mean([b["close"] for b in bars[-10:]]), "sma20": mean([b["close"] for b in bars[-20:]]), "automatic_exit": False}
            liquidity = mean([b["volume"] * b["close"] for b in prior[-20:]])
            row["dollar_volume_20d"] = liquidity
            if liquidity < cfg["min_dollar_volume"]:
                row["reasons"].append("low/unknown liquidity: research only")
            events = triggers(intra, "sell" if direction == "short" else "buy", cfg, mode == MODES[2]) if active else []
            if mode == MODES[2] and cap is not None:
                cap_records = [r for r in evidence.get("metadata", []) if r.get("symbol") == sym
                               and r.get("reference") and available(r, now) and positive(r.get("market_cap"))]
                cap_available = max(available(r, now) for r in cap_records)
                # A later cap snapshot cannot classify an earlier trigger.
                events = [e for e in events if timestamp(e["at"]) >= cap_available]
            if mode == MODES[0]:
                # A base break needs its own level as well as an OR break.
                events = [e for e in events if e["price"] > metrics.get("base_high", float("inf"))]
                if metrics.get("daily_breakout"):
                    row["daily_breakout"] = {"confirmed": True, "available_at": iso(clock.eastern_to_utc(dt.datetime.combine(bars[-1]["date"], clock.session(bars[-1]["date"])[1]))), "current_entry": False}
            apply_trigger(row, events, intra, catalysts, cfg)
            if intra:
                row["price"] = intra[-1]["close"]
                if mode != MODES[1]:
                    row["volume"] = sum(b["volume"] for b in intra) if all(b["volume"] is not None for b in intra) else None
                    row["rvol"] = row["volume"] / adv if row["volume"] is not None and adv else None
            row["volume_basis"] = "current completed regular-session bars / prior ADV20" if intra else "last completed daily session"
            row["extended_context"] = {"bars": len(extended), "as_of": iso(extended[-1]["end"]) if extended else None,
                                       "latest_price": extended[-1]["close"] if extended else None}
            stop = row["stop"]
            row["stop_context"] = {"basis": "HIGH_OF_DAY" if direction == "short" else "LOW_OF_DAY", "price": (max(b["high"] for b in intra) if direction == "short" else min(b["low"] for b in intra)) if intra else None}
            row["risk"] = risk.scanner_stop_context(row["trigger"]["price"] if row["trigger"] else row["price"], stop["price"] if stop else row["stop_context"]["price"], "sell" if direction == "short" else "buy", row["atr"], row["adr"], cfg["stop_warning_multiple"], risk_cfg, sym)
            if row["setup_status"] in ("CANDIDATE", "SETUP_READY", "TRIGGERED"):
                row["action"] = "WATCH"
            row["observed_triggers"] = events
            if mode == MODES[0] and row["risk"]["preferred"] is False:
                row["reasons"].append("breakout stop exceeds both 1x ATR and ADR; WATCH")
            if row["risk"]["warning"]:
                row["reasons"].append("stop exceeds configured volatility warning threshold; WATCH")
            if (row["setup_status"] == "TRIGGERED" and not row["risk"]["blocks"] and not row["risk"]["warning"]
                    and (mode != MODES[0] or row["risk"]["preferred"] is True) and liquidity >= cfg["min_dollar_volume"]):
                row["action"] = "SHORT" if direction == "short" else "LONG"
            row["reasons"].extend(row["risk"]["blocks"])
            if not row["reasons"]:
                row["reasons"].append("measurable setup proxy matched; independent entry/exit/budget/risk checks still required")
            results.append(row)
    status_order = {s: i for i, s in enumerate(("TRIGGERED", "SETUP_READY", "CANDIDATE", "FAILED", "INSUFFICIENT_DATA"))}
    results.sort(key=lambda r: (status_order[r["setup_status"]], -r["setup_quality"], r["symbol"], r["variant"]))
    counts = {"requested": len(daily), "fetched": sum(sym in data for sym in daily), "eligible": sum(bool(v) for v in daily.values()),
              "ranked": {p: len(v) for p, v in ranks.items()},
              "statuses": {s: sum(r["setup_status"] == s for r in results) for s in ("CANDIDATE", "SETUP_READY", "TRIGGERED", "FAILED", "INSUFFICIENT_DATA")}}
    return results, counts
