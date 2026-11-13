"""Read unchanged daily states, confirmed events and executable trigger plans."""
import bisect
import datetime as dt
from pathlib import Path

import numpy as np

from common import iso, module, read, stamp
from daily_history import mapped
from engine import exit_price
from event_signals import BITS, NAMES, VARIANTS, extract_events

PLAN_NAMES = {"MINERVINI_ENTRY": "MINERVINI", "QULLAMAGGIE_BREAKOUT": "BREAKOUT_SETUP_READY",
              "CONFLUENCE_TRIGGERED": "CONFLUENCE_SETUP_READY"}


class Timeline:
    """Trading minutes, retaining empty sessions and exceptional early closes."""
    def __init__(self, market, start, end):
        self.days = market.timeline(start, end)
        self.opens, self.closes, self.starts, self.ends = [], [], [], []
        total = 0
        for day in self.days:
            date = dt.date.fromisoformat(day)
            opening = market.clock.eastern_to_utc(dt.datetime.combine(date, market.clock.session(date)[0]))
            closing = market.clock.close(day)
            self.opens.append(opening)
            self.closes.append(closing)
            self.starts.append(total)
            total += (closing - opening).total_seconds() / 60
            self.ends.append(total)
        self.total = total

    def at(self, when):
        when = stamp(when) if isinstance(when, str) else when
        i = bisect.bisect_right(self.opens, when) - 1
        if i < 0 or when > self.closes[i]:
            raise ValueError("timestamp outside portfolio timeline")
        return self.starts[i] + (when - self.opens[i]).total_seconds() / 60


def daily_events(market, start, end):
    """Small-snapshot adapter; native scanners and existing event extractor.

    The common 260-session cohort and rank population match the daily study.
    Pre-study known states are retained so the study boundary creates no event.
    """
    mini = module("trader-scanner", "minervini")
    qull = module("trader-scanner", "qullamaggie")
    cf = module("trader-confluence", "confluence")
    first = min((r["day"] for s in market.universe for r in market.bars(s, "1d")), default=start)
    days = market.timeline(first, end)
    shape = (len(days), len(market.universe))
    states, eligible = np.zeros(shape, dtype="uint8"), np.zeros(shape, dtype=bool)
    details = {}
    for d, day in enumerate(days):
        now = market.clock.close(day)
        data = {s: market.series(s, "1d", now) for s in market.universe if market.complete_history(s, now, 260)}
        if not data:
            continue
        mrows, _ = mini.scan(data, market.universe, now, market.clock)
        qrows, _ = qull.scan(data, market.universe, now, market.clock, "qullamaggie-breakout")
        mrows, qrows = {r["symbol"]: r for r in mrows}, {r["symbol"]: r for r in qrows}
        for i, sym in enumerate(market.universe):
            if sym not in data:
                continue
            eligible[d, i] = True
            m, q = mrows[sym], qrows[sym]
            bits = 1 if m["trend_template"] == "PASS" else 0
            status = q["setup_status"]
            bits |= 4 if status == "SETUP_READY" else 2 if status == "CANDIDATE" else 0
            _, level = cf.classify(m["trend_template"], {"setup": "BREAKOUT", "status": status})
            if level:
                bits |= 16 if status == "SETUP_READY" else 8
            states[d, i] = bits
            details[d, i] = {"RS_PROXY": m.get("RS_PROXY"), "liquidity": q.get("dollar_volume_20d") or 0}
    streams = {v: [] for v in VARIANTS}
    for i, sym in enumerate(market.universe):
        full, cooled, _, _ = extract_events(states[:, i], eligible[:, i])
        for variant, array in zip(VARIANTS, (full, cooled)):
            for name, bit in zip(NAMES, BITS):
                for d in np.flatnonzero(array & bit):
                    if days[d] >= start:
                        streams[variant].append({"id": "/".join((variant, name, sym, days[d])), "strategy": name,
                            "symbol": sym, "day": days[d], "at": iso(market.clock.close(days[d])),
                            "status": "PASS" if name == "MINERVINI" else "SETUP_READY", **details[d, i]})
    return {"days": days, "universe": market.universe, "states": states, "eligible": eligible, "events": streams}


def matrix_events(analysis, events, market):
    """Consume the previously frozen large event matrices without a new scan."""
    analysis, events = Path(analysis), Path(events)
    meta = read(analysis / "matrix-metadata.json")
    emeta = read(events / "event-metadata.json")
    if any(meta[k] != emeta[k] for k in ("days", "shape", "universe", "acquisition_sha256")):
        raise ValueError("event matrices do not match original analysis")
    if set(meta["universe"]) != set(market.universe):
        raise ValueError("execution universe and event matrix universe differ")
    shape = meta["shape"]
    states = mapped(analysis / "signals.u8", shape, "uint8")
    eligible = mapped(analysis / "eligible.u8", shape, "uint8")
    ranks = mapped(analysis / "ranks.f64", (4,) + tuple(shape), "float64")
    streams = {}
    for variant in VARIANTS:
        streams[variant] = []
        array = mapped(events / ("events-" + variant + ".u8"), shape, "uint8")
        for name, bit in zip(NAMES, BITS):
            for d, i in zip(*np.nonzero(array & bit)):
                sym, day = meta["universe"][i], meta["days"][d]
                # Liquidity must come from information at the event, never a future mark.
                rows = market.bars(sym, "1d", market.clock.close(day))[-20:]
                liquid = sum(r["c"] * r["v"] for r in rows) / len(rows) if rows else 0
                streams[variant].append({"id": "/".join((variant, name, sym, day)), "strategy": name,
                    "symbol": sym, "day": day, "at": iso(market.clock.close(day)),
                    "status": "PASS" if name == "MINERVINI" else "SETUP_READY",
                    "RS_PROXY": float(ranks[3, d, i]) if np.isfinite(ranks[3, d, i]) else None, "liquidity": liquid})
    return {"days": meta["days"], "universe": meta["universe"], "states": states,
            "eligible": eligible, "events": streams}


def bind_plans(source, plans, variant, market):
    """First confirmed trigger AFTER the event, before reset/unknown/new event.

    No observed-late replacement for a possibly missing earlier trigger. Full
    intraday coverage is required from the session after the daily event.
    Consumed events are never retried after risk/slot rejection.
    """
    days, symbols = source["days"], source["universe"]
    index = {d: i for i, d in enumerate(days)}
    grouped, used, linked = {}, set(), []
    for e in source["events"][variant]:
        grouped.setdefault((e["strategy"], e["symbol"]), []).append(e)
    for group in grouped.values():
        group.sort(key=lambda e: e["at"])
    for plan in sorted(plans, key=lambda p: (p["at"], p["id"])):
        key = (PLAN_NAMES.get(plan["strategy"]), plan["symbol"])
        candidates = [e for e in grouped.get(key, []) if stamp(e["at"]) <= stamp(plan["at"])]
        if not candidates:
            continue
        event = candidates[-1]
        if event["id"] in used:
            continue
        a, b, i = index[event["day"]], index[plan["day"]], symbols.index(plan["symbol"])
        before = source["states"][a:b, i]
        known = source["eligible"][a:b, i]
        if key[0] == "MINERVINI":
            reset = (before & 1) == 0
        elif key[0] == "BREAKOUT_SETUP_READY":
            reset = (before & 6) == 0
        else:
            reset = ((before & 1) == 0) | ((before & 6) == 0)
        if not np.all(known) or np.any(reset):
            continue
        contiguous = True
        for day in days[a + 1:b + 1]:
            rows = market.execution_bars(plan["symbol"], "1m", day) or market.execution_bars(plan["symbol"], "5m", day)
            opening = market.clock.eastern_to_utc(dt.datetime.combine(dt.date.fromisoformat(day), market.clock.session(dt.date.fromisoformat(day))[0]))
            required_end = stamp(plan["at"]) if day == plan["day"] else market.clock.close(day)
            prefix = [r for r in rows if r["_start"] < required_end]
            if not prefix or prefix[0]["_start"] != opening or prefix[-1]["_end"] != required_end or any(
                    x["_end"] != y["_start"] for x, y in zip(prefix, prefix[1:])):
                contiguous = False
                break
        if contiguous:
            used.add(event["id"])
            linked.append(dict(plan, id=event["id"], strategy=key[0], trigger_id=plan["id"],
                               event_at=event["at"], trigger_setup_status=3,
                               RS_PROXY=event["RS_PROXY"], liquidity=event["liquidity"]))
    missing = [dict(e, reason="NO_COMPLETE_POST_EVENT_TRIGGER_PLAN") for e in source["events"][variant] if e["id"] not in used]
    return linked, missing


def reconstruct(market, plans, config, fx, timeline):
    """Freeze observed, complete mid-price paths; no artificial stops or exits."""
    templates, rejected = [], []
    for p in plans:
        try:
            now = stamp(p["at"])
            if stamp(p["stop_known_at"]) > now or p.get("instrument_type", "stock") != "stock":
                raise ValueError("STOP_FROM_FUTURE_OR_UNSUPPORTED_CONTRACT")
            frame = "1m" if market.bars(p["symbol"], "1m", day=p["day"]) else "5m"
            rows = [r for r in market.execution_bars(p["symbol"], frame, p["day"]) if r["_start"] >= now]
            if not rows or rows[0]["_start"] != now:
                raise ValueError("NEXT_INTRADAY_BAR_MISSING")
            factor = market.basis_factor(p["symbol"], now)
            entry = rows[0]["o"] * factor
            sign = -1 if p["side"] == "short" else 1
            if sign * (entry - p["stop"]) <= 0 or sign * (p["target"] - entry) <= 0:
                raise ValueError("OPEN_OUTSIDE_BRACKET")
            if config["max_hold_sessions"] != 1:
                raise ValueError("NATIVE_RECONSTRUCTION_REQUIRES_EXPLICIT_MULTIDAY_EXIT_POLICY")
            # Native exit shifts flat_by by three hours on early-close days.
            hours = market.clock.session(dt.date.fromisoformat(p["day"]))
            flat = dt.datetime.combine(dt.date.fromisoformat(p["day"]), dt.time.fromisoformat(config["flat_by"]))
            if hours[1] == dt.time(13):
                flat -= dt.timedelta(hours=3)
            cutoff = market.clock.eastern_to_utc(flat)
            if now >= cutoff:
                raise ValueError("AFTER_FLAT_BY")
            marks, previous, terminal = [], now, None
            for r in rows:
                if r["_start"] != previous:
                    raise ValueError("INCOMPLETE_POSITION_HISTORY")
                previous = r["_end"]
                b = {k: r[k] * factor for k in ("o", "h", "l", "c")}
                mid, reason = exit_price(b, p["stop"], p["target"], p["side"])
                if mid is None and r["_end"] >= cutoff:
                    mid, reason = b["c"], "TIME_EXIT"
                marks.append([timeline.at(r["_end"]), b["c"], fx.at(r["_end"])])
                if mid is not None:
                    terminal = (r, mid, reason)
                    break
            if terminal is None:
                raise ValueError("NO_COMPLETE_EXIT_HISTORY")
            r, mid, reason = terminal
            templates.append({k: p[k] for k in ("id", "strategy", "symbol", "side", "stop", "target", "RS_PROXY", "liquidity", "trigger_setup_status")})
            templates[-1].update(entry_t=timeline.at(now), exit_t=timeline.at(r["_end"]), entry_mid=entry,
                exit_mid=mid, entry_fx=fx.at(now), exit_fx=fx.at(r["_end"]), marks=marks,
                entry_at=iso(now), exit_at=iso(r["_end"]), event_at=p["event_at"], exit_reason=reason)
        except (ValueError, KeyError, TypeError) as exc:
            rejected.append({"id": p.get("id"), "reason": str(exc)})
    return templates, rejected


def forward_events(market, events, horizon, end):
    daily = {s: {r["day"]: r for r in market.bars(s, "1d")} for s in market.universe}
    out = []
    for event in events:
        pos = market.days.index(event["day"]) + horizon
        last = market.days[pos] if pos < len(market.days) else None
        window = market.days[pos - horizon:pos + 1]
        bars = daily[event["symbol"]]
        row = dict(event, horizon=horizon, return_pct=None, issue=None)
        if last is None or last > end:
            row["issue"] = "RIGHT_CENSORED"
        elif any(d not in bars for d in window):
            row["issue"] = "MISSING_HISTORY"
        else:
            row["return_pct"] = 100 * (bars[last]["c"] / bars[event["day"]]["c"] - 1)
        out.append(row)
    return out
