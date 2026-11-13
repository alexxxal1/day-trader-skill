"""Replay unchanged Qullamaggie/Minervini functions on causal intraday prefixes."""
import copy

import numpy as np

from common import EPOCH, iso, module, stamp
from event_quality_pit import CAUSAL_BITS, causal_bar_flags
from true_trade_execution import contiguous_prefix, native_bars, opening, session_rows

QULL = module("trader-scanner", "qullamaggie")
MINI = module("trader-scanner", "minervini")
CF = module("trader-confluence", "confluence")
STRATEGIES = ("BREAKOUT", "EP", "PARABOLIC_SHORT", "PARABOLIC_LONG", "CONFLUENCE_TRIGGERED")
MODES = {"BREAKOUT": "qullamaggie-breakout", "EP": "qullamaggie-ep",
         "PARABOLIC_SHORT": "qullamaggie-parabolic", "PARABOLIC_LONG": "qullamaggie-parabolic"}
# Permission to study paper shorts, not changed strategy thresholds or broker authorization.
PAPER_RISK = {"allow_short": True, "allow_margin": True, "paper": True}


def quality_at(market, symbol, now, lookback=260):
    """Use exactly the existing causal OHLCV kernel, without future metadata."""
    days = [d for d in market.days if market.clock.close(d) <= now][-lookback:]
    by_day = {r["day"]: r for r in market.bars(symbol, "1d", now)}
    arrays = {k: [by_day[d][k] if d in by_day else np.nan for d in days] for k in ("o", "h", "l", "c", "v")}
    arrays["present"] = [d in by_day for d in days]
    flags = causal_bar_flags(arrays, {"daily_gross_jump_factor": 2.0, "stale_zero_volume_sessions": 20})
    bits = int(np.bitwise_or.reduce(flags, initial=np.uint32(0)))
    return {"pit_safe_status": "PIT_SAFE" if len(days) == lookback and not bits & CAUSAL_BITS else "PIT_REJECTED",
            "pit_reasons": bits, "quality_as_of": iso(now),
            "data_quality_status": "NOT_HISTORICALLY_PIT_CERTIFIED" if market.document.get("provenance", {}).get("retrospective_snapshot") else "VINTAGE_ARCHIVE_UNVERIFIED"}


def catalysts_before(market, symbol, when):
    """Strictly BEFORE confirmation. Quiver disclosure and revisions included."""
    evidence = market.events(when)
    evidence["events"] = [r for r in evidence.get("events", [])
        if r.get("symbol") == symbol and stamp(r["published_at"]) < when and stamp(r["available_at"]) < when]
    day = market.clock.to_eastern(when).date()
    catalysts, _, _ = QULL.evidence_for(evidence, symbol, when, day, market.clock, QULL.config())
    return evidence, catalysts


def series_from_prefix(rows):
    # Rows have already been put onto this session's decision-time basis.
    return {**{k: [r[k] for r in rows] for k in ("o", "h", "l", "c", "v")},
        "t": [(r["_start"] - EPOCH).total_seconds() for r in rows],
        "meta": {"dataGranularity": rows[0]["interval"]}, "price_basis": "RAW", "splits": {}}


def replay(market, config, start, end):
    plans, audits, coverage = [], [], []
    cfg = QULL.config()
    for day in market.timeline(start, end):
        op = opening(market.clock, day)
        # Population is the same common 260-session cohort as the Daily layer.
        daily = {s: market.series(s, "1d", op) for s in market.universe if market.complete_history(s, op, 260)}
        trends = {r["symbol"]: r for r in MINI.scan(daily, market.universe, op, market.clock)[0]}
        before = {}
        for mode in ("qullamaggie-breakout", "qullamaggie-parabolic"):
            for row in QULL.scan(daily, market.universe, op, market.clock, mode,
                                  evidence=market.events(op), risk_cfg=PAPER_RISK)[0]:
                before[row["symbol"], row["variant"]] = row
        for symbol in market.universe:
            frame, rows = session_rows(market, symbol, day)
            prefix = contiguous_prefix(rows, op)
            bars = native_bars(prefix, market.clock)
            coverage.append({"ticker": symbol, "session": day, "frame": frame, "regular_bars": len(rows),
                "contiguous_opening_bars": len(prefix), "last_contiguous_end": iso(prefix[-1]["_end"]) if prefix else None,
                "complete_session": bool(prefix and prefix[-1]["_end"] == market.clock.close(day)),
                "one_minute_or_resolvable": frame == "1m" and bool(prefix)})
            quality = quality_at(market, symbol, op)
            prepared, _ = QULL.prepare(daily[symbol], op, market.clock) if symbol in daily else ([], "missing daily history")
            previous_day = prepared[-1]["date"].isoformat() if prepared else None
            for strategy in STRATEGIES[:-1]:
                row = before.get((symbol, strategy), {})
                status = row.get("setup_status", "INSUFFICIENT_DATA")
                setup_date, setup_at = previous_day, iso(market.clock.close(previous_day)) if previous_day else iso(op)
                if strategy == "EP" and prepared and bars:
                    # The current gap is known only once the opening print exists.
                    status, _, _, _ = QULL.ep(prepared, bars[:1], [], [], cfg)
                    setup_date, setup_at = day, iso(prefix[0]["_end"])
                audit = {"id": "/".join((strategy, symbol, day)), "strategy": strategy, "ticker": symbol,
                    "session": day, "setup_date": setup_date, "setup_known_at": setup_at,
                    "native_setup_status": status, "setup_ready": status == "SETUP_READY", "candidate": status == "CANDIDATE",
                    "triggered": False, "reason": None, "frame": frame, **quality}
                audits.append(audit)
                confl = None
                if strategy == "BREAKOUT":
                    _, level = CF.classify(trends[symbol]["trend_template"], {"setup": "BREAKOUT", "status": status})
                    confl = dict(audit, id="CONFLUENCE_TRIGGERED/" + symbol + "/" + day,
                        strategy="CONFLUENCE_TRIGGERED", minervini_status=trends[symbol]["trend_template"],
                        setup_ready=bool(level and status == "SETUP_READY"), candidate=bool(level and status == "CANDIDATE"),
                        native_setup_status=status if level else "NOT_CONFLUENT",
                        reason="MINERVINI_NOT_PASS" if trends[symbol]["trend_template"] != "PASS" else "NOT_SETUP_READY" if status != "SETUP_READY" else None)
                    audits.append(confl)
                targets = [audit] + ([confl] if confl and confl["setup_ready"] else [])

                def reject(reason):
                    for a in targets:
                        a["reason"] = reason

                if status not in ("CANDIDATE", "SETUP_READY") or (strategy != "EP" and status != "SETUP_READY"):
                    reject("NOT_SETUP_READY" if status != "INSUFFICIENT_DATA" else "INSUFFICIENT_DAILY_DATA")
                    continue
                if quality["pit_safe_status"] != "PIT_SAFE":
                    reject("PIT_SAFE_REJECTED")
                    continue
                if not bars:
                    reject("INSUFFICIENT_INTRADAY_DATA")
                    continue
                events = QULL.triggers(bars, "sell" if strategy == "PARABOLIC_SHORT" else "buy", cfg, strategy.startswith("PARABOLIC"))
                if strategy == "BREAKOUT":
                    events = [e for e in events if e["price"] > row["metrics"]["base_high"]]
                selected = None
                ep_catalyst_seen = False
                for event in events:
                    now = stamp(event["at"])
                    if now >= market.clock.close(day):
                        continue
                    evidence, catalysts = catalysts_before(market, symbol, now)
                    ep_catalyst_seen |= bool(catalysts)
                    if strategy == "EP" and not catalysts:
                        continue
                    exact_prefix = prefix[:event["index"] + 1]
                    # The existing causal zero-volume/changed-close check is
                    # also observable on this prefix. Never use later bars.
                    intraday_flags = causal_bar_flags({k: [r[k] for r in exact_prefix] for k in ("o", "h", "l", "c", "v")},
                        {"daily_gross_jump_factor": 2.0, "stale_zero_volume_sessions": 20})
                    if np.any(intraday_flags & (1 | 4)):
                        reject("PIT_SAFE_INTRADAY_REJECTED")
                        break
                    source = {symbol: {frame: series_from_prefix(exact_prefix)}}
                    candidates = QULL.scan(daily, market.universe, now, market.clock, MODES[strategy],
                        intraday=source, evidence=evidence, risk_cfg=PAPER_RISK)[0]
                    native = next(r for r in candidates if r["symbol"] == symbol and r["variant"] == strategy)
                    if native["setup_status"] != "TRIGGERED" or native["trigger"]["at"] != iso(now):
                        continue
                    # Retain actual structural triggers even when execution risk rejects them.
                    for a in targets:
                        a.update(triggered=True, trigger_time=iso(now), trigger_type=native["trigger"]["type"],
                                 setup_ready=True, native_risk=native["risk"])
                    selected = native
                    break
                if selected is None:
                    if audit["reason"] is None:
                        reject("INSUFFICIENT_CATALYST_DATA" if strategy == "EP" and not ep_catalyst_seen else
                               "INSUFFICIENT_VOLUME_AT_TRIGGER" if strategy == "EP" else "NO_CONFIRMED_TRIGGER")
                    continue
                if selected["action"] not in ("LONG", "SHORT"):
                    reject("NATIVE_SCANNER_RISK_BLOCK")
                    continue
                if not selected.get("stop"):
                    reject("INSUFFICIENT_STOP_DATA")
                    continue
                stop = selected["stop"]
                plan = {"id": audit["id"], "ticker": symbol, "symbol": symbol, "strategy": strategy,
                    "session": day, "setup_date": setup_date, "setup_known_at": setup_at,
                    "trigger_type": selected["trigger"]["type"], "trigger_time": selected["trigger"]["at"],
                    "trigger_price": selected["trigger"]["price"], "trigger_level": selected["trigger"]["level"],
                    "stop_initial": stop["price"], "stop_known_at": stop["as_of"],
                    "stop_basis": "VWAP_RECLAIM_AT_TRIGGER" if stop["basis"] == "VWAP reclaim" else
                                  "SESSION_HIGH_THROUGH_TRIGGER" if strategy == "PARABOLIC_SHORT" else "SESSION_LOW_THROUGH_TRIGGER",
                    "side": "short" if strategy == "PARABOLIC_SHORT" else "long", "frame": frame,
                    "catalyst_status": "VERIFIED_BEFORE_TRIGGER" if strategy == "EP" else "NOT_REQUIRED",
                    "catalysts": selected["catalyst"] if strategy == "EP" else [],
                    "RS_PROXY": trends[symbol].get("RS_PROXY"), "liquidity": selected["dollar_volume_20d"],
                    "atr": selected["atr"], "adr": selected["adr"],
                    "ambiguity_flags": ["MODELLED_COSTS_NOT_OBSERVED_HISTORICAL_COSTS"], **quality}
                plans.append(plan)
                if confl and confl["triggered"]:
                    plans.append(dict(copy.deepcopy(plan), id=confl["id"], strategy="CONFLUENCE_TRIGGERED",
                                      minervini_status="PASS", parent_breakout_id=plan["id"]))
        print("True-trigger replay", day, "plans", len(plans), flush=True)
    return {"plans": plans, "audit": audits, "coverage": coverage}
