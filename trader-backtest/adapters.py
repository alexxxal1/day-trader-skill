"""Read-only replay of the existing strategies; no duplicate strategy criteria."""
import datetime as dt
from decimal import Decimal

from common import EPOCH, iso, module

STRATEGIES = ("MINERVINI_FILTER", "MINERVINI_ENTRY", "QULLAMAGGIE_BREAKOUT", "EPISODIC_PIVOT",
              "PARABOLIC_SHORT", "PARABOLIC_LONG", "CONFLUENCE_CANDIDATE",
              "CONFLUENCE_SETUP_READY", "CONFLUENCE_TRIGGERED")


def replay(market, config):
    mini = module("trader-scanner", "minervini")
    qull = module("trader-scanner", "qullamaggie")
    entry = module("trader-entry", "entry")
    exits = module("trader-exit", "exit")
    confluence = module("trader-confluence", "confluence")
    clock, universe = market.clock, market.universe
    if any(int(d[:4]) not in entry.CALENDAR_YEARS for d in market.timeline(config["start"], config["end"])):
        raise ValueError("Strategy evaluation requires the existing entry/confluence calendar (2026-2027); earlier data is warmup only")
    signals, plans, rejections = [], [], []
    emitted, planned = set(), set()

    def emit(strategy, row, now, trend=None, status=None):
        status = status or row.get("setup_status", "PASS")
        day = clock.to_eastern(now).date().isoformat()
        key = (strategy, row["symbol"], day, status)
        if key in emitted:
            return None
        emitted.add(key)
        signal = {"id": "/".join(key), "strategy": strategy, "symbol": row["symbol"],
                  "at": iso(now), "day": day, "status": status,
                  "side": "short" if row.get("variant") == "PARABOLIC_SHORT" else "long",
                  "reference_price": row.get("price"), "RS_PROXY": (trend or row).get("RS_PROXY"),
                  "liquidity": row.get("dollar_volume_20d", 0),
                  "confluence_level": {"TRIGGERED": 3, "SETUP_READY": 2, "CANDIDATE": 1}.get(status, 0)
                  if strategy.startswith("CONFLUENCE") else 0}
        signals.append(signal)
        return signal

    def plan(signal, stop, now, reason=None):
        if not signal:
            return
        key = (signal["strategy"], signal["symbol"], signal["day"])
        if key in planned:
            return
        if reason:
            rejections.append({"signal": signal["id"], "reason": reason})
            return
        try:
            values = exits.compute(signal["reference_price"], "price", Decimal(str(stop)),
                                   "r", Decimal(str(config["target_r"])), side=signal["side"])
        except (exits.PlanError, ValueError, TypeError) as exc:
            rejections.append({"signal": signal["id"], "reason": str(exc)})
            return
        planned.add(key)
        plans.append(dict(signal, entry_ref=float(values["entry"]), stop=float(values["stop"]),
                          target=float(values["target"]), instrument_type="stock",
                          exit_policy="existing trader-exit fixed stop + configured R target; scheduled flat_by",
                          stop_known_at=iso(now)))

    def prepare(now):
        data = market.daily(now)
        # Missing sessions cannot silently shorten a moving-average window.
        mdata = {s: v for s, v in data.items() if market.complete_history(s, now, 260)}
        qdata = {s: v for s, v in data.items() if market.complete_history(s, now, 127)}
        mrows, counts = mini.scan(mdata, universe, now, clock)
        return qdata, {r["symbol"]: r for r in mrows}, counts

    def consume(rows, trends, now):
        for row in rows:
            status, sym = row["setup_status"], row["symbol"]
            trend = trends[sym]
            if status not in ("CANDIDATE", "SETUP_READY", "TRIGGERED"):
                continue
            variant = row["variant"]
            strategy = {"BREAKOUT": "QULLAMAGGIE_BREAKOUT", "EP": "EPISODIC_PIVOT",
                        "PARABOLIC_SHORT": "PARABOLIC_SHORT", "PARABOLIC_LONG": "PARABOLIC_LONG"}[variant]
            signal = None
            if status in ("SETUP_READY", "TRIGGERED") and (variant in ("BREAKOUT", "EP") or status == "TRIGGERED"):
                signal = emit(strategy, row, now, trend)
            if status == "TRIGGERED":
                # Never backdate a trigger discovered by a later snapshot.
                exact = row.get("trigger", {}).get("at") == iso(now)
                reason = None if exact and row.get("action") in ("LONG", "SHORT") else "TRIGGER_NOT_CURRENT_OR_SCANNER_RISK_BLOCK"
                plan(signal, (row.get("stop") or {}).get("price"), now, reason)
            if variant == "BREAKOUT":
                _, level = confluence.classify(trend["trend_template"], {"setup": variant, "status": status})
                if level:
                    cs = emit("CONFLUENCE_" + status, row, now, trend)
                    if status == "TRIGGERED":
                        plan(cs, (row.get("stop") or {}).get("price"), now, reason)

    diagnostics = []
    for day in market.timeline(config["start"], config["end"]):
        close = clock.close(day)
        opening = clock.eastern_to_utc(dt.datetime.combine(dt.date.fromisoformat(day), clock.session(dt.date.fromisoformat(day))[0]))
        qdata, trends, _ = prepare(opening)
        before = {}
        for mode in qull.MODES:
            rows, _ = qull.scan(qdata, universe, opening, clock, mode, evidence=market.events(opening))
            before[mode] = rows
        # Replay every completed intraday bar. Skip a mode only when its
        # completed daily state cannot trigger, or no EP evidence exists.
        frames = {s: "1m" if market.bars(s, "1m", day=day) else "5m" for s in universe}
        times = sorted({r["_end"] for s in universe for r in market.bars(s, frames[s], day=day)
                        if opening < r["_end"] < close})
        for now in times:
            intra = {s: market.series(s, frames[s], now) for s in universe}
            evidence = market.events(now)
            for mode in qull.MODES:
                ready = any(r["setup_status"] == "SETUP_READY" for r in before[mode])
                if mode == "qullamaggie-ep":
                    ready = bool(evidence["events"])
                if ready:
                    rows, _ = qull.scan(qdata, universe, now, clock, mode, intraday=intra, evidence=evidence)
                    consume(rows, trends, now)
            # Existing ORB entry pipeline, a complete contiguous prefix only.
            for sym in universe:
                if trends[sym]["trend_template"] != "PASS":
                    continue
                bars, _, error = qull.intraday_bars(intra[sym], now, clock)
                if error or not bars:
                    continue
                chart = {"symbol": sym, "bars": [dict(t=int((clock.eastern_to_utc(b["start"]) - EPOCH).total_seconds()),
                         o=b["open"], h=b["high"], l=b["low"], c=b["close"], v=b["volume"] or 0) for b in bars]}
                result = entry.evaluate(chart, now, interval=frames[sym])
                trigger = result.get("trigger")
                if result["status"] == "triggered" and trigger["bar_index"] == len(bars) - 1:
                    row = dict(trends[sym], price=trigger["price"])
                    signal = emit("MINERVINI_ENTRY", row, now, status="TRIGGERED")
                    plan(signal, result["opening_range"]["low"], now)
        qdata, trends, counts = prepare(close)
        diagnostics.append({"day": day, "minervini": counts, "intraday_snapshots": len(times)})
        for row in trends.values():
            if row["trend_template"] == "PASS":
                emit("MINERVINI_FILTER", row, close)
        for mode in qull.MODES:
            rows, _ = qull.scan(qdata, universe, close, clock, mode, evidence=market.events(close))
            consume(rows, trends, close)
    return {"signals": sorted(signals, key=lambda s: (s["at"], s["id"])),
            "plans": sorted(plans, key=lambda s: (s["at"], s["id"])),
            "rejections": rejections, "diagnostics": diagnostics}
