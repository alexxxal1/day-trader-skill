#!/usr/bin/env python3
"""Transparent Minervini/Qullamaggie confluence, offline and analysis only."""

import argparse
import copy
import datetime as dt
import importlib.util
import json
import math
import os
import re
import sys


def local_module(folder, name):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("confluence_" + name, os.path.join(root, folder, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ev = local_module("trader-confluence", "evidence")
clock = local_module("trader-entry", "entry")
STATUSES = ("CANDIDATE", "SETUP_READY", "TRIGGERED", "FAILED", "INSUFFICIENT_DATA")
VARIANTS = ("BREAKOUT", "EP", "PARABOLIC_SHORT", "PARABOLIC_LONG")
ACTIVE = ("CANDIDATE", "SETUP_READY", "TRIGGERED")
STAGES = {"INSUFFICIENT_DATA": 0, "FAILED": 1, "CANDIDATE": 2, "SETUP_READY": 3, "TRIGGERED": 4}
NOTE = ("Confluence is analysis only, never an order or risk approval. No broker connection. "
        "Current entry/exit/budget/risk checks still decide; Minervini PASS alone is never an entry.")


def positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def price(value):
    value = value.get("price") if isinstance(value, dict) else value
    return value if positive(value) else None


def daily_close(value):
    try:
        day = dt.datetime.strptime(value, "%Y-%m-%d").date()
        hours = clock.session(day)
        if not hours or day.year not in (2026, 2027):
            return None
        return clock.eastern_to_utc(dt.datetime.combine(day, hours[1])).replace(tzinfo=ev.UTC)
    except (ValueError, TypeError):
        return None


def last_completed(point):
    local = clock.to_eastern(point.replace(tzinfo=None))
    day = local.date()
    for _ in range(14):
        close = daily_close(day.isoformat())
        if close and close <= point:
            return close
        day -= dt.timedelta(days=1)
    return None


def latest(records, now, channel):
    """Never rewind a later report into a past decision; keep rejection reasons."""
    rejected, known = [], []
    for record in records:
        issue = ev.temporal(record["at"], now, channel) or ev.temporal(record.get("data_at", record["at"]), now, channel)
        if issue:
            rejected.append(issue)
        else:
            known.append(record)
    if not known:
        return None, sorted(set(rejected or ["MISSING_REPORT"]))
    at = max(r["at"] for r in known)
    newest = [r for r in known if r["at"] == at]
    if len({json.dumps(r["row"], sort_keys=True) for r in newest}) > 1:
        return None, ["CONTRADICTORY_SNAPSHOTS_AT_SAME_TIME"]
    return newest[0], []


def trend_component(records, now):
    record, issues = latest(records, now, "MINERVINI")
    status = "INSUFFICIENT_DATA"
    if record:
        row = record["row"]
        close = daily_close(row.get("close_date"))
        criteria = row.get("criteria") or {}
        reason = str(row.get("reason") or "")
        if close is None or close > record["at"]:
            issues.append("DAILY_CLOSE_NOT_AVAILABLE_AT_SNAPSHOT")
        elif close != last_completed(now):
            issues.append("STALE_DAILY_DATA")
        elif (row.get("data_error") or reason.startswith(("INSUFFICIENT_DATA", "STALE_DATA"))
              or any(v is None for v in criteria.values())):
            issues.append(reason or "INCOMPLETE_TREND_CRITERIA")
        elif row.get("trend_template") in ("PASS", "FAIL"):
            status = row["trend_template"]
        else:
            issues.append("UNKNOWN_TREND_STATUS")
    return status, {"status": status, "as_of": ev.iso(record["at"]) if record else None,
                    "issues": issues, "source": copy.deepcopy(record["row"]) if record else None}


def trigger_issue(trigger, stop, snapshot_at):
    if not isinstance(trigger, dict):
        return "MISSING_COMPLETED_BAR_TRIGGER"
    at = ev.timestamp(trigger.get("at"))
    if at is None or at > snapshot_at or trigger.get("current") is not True or price(trigger) is None:
        return "TRIGGER_NOT_CURRENT_OR_NOT_AVAILABLE"
    local = clock.to_eastern(at.replace(tzinfo=None))
    hours = clock.session(local.date())
    snapshot_local = clock.to_eastern(snapshot_at.replace(tzinfo=None))
    if (not hours or local.date() != snapshot_local.date() or not hours[0] < local.time() <= hours[1]
            or snapshot_local.time() >= hours[1]):
        return "NO_CURRENT_REGULAR_SESSION_TRIGGER"
    if local.second or local.microsecond:
        return "TRIGGER_BAR_NOT_CLOSED"
    kind = trigger.get("type", "")
    opening = dt.datetime.combine(local.date(), hours[0])
    match = re.fullmatch(r"OR[HL]_(1|5|60)m", kind)
    if match:
        minutes = int(match.group(1))
        # First completed bar strictly AFTER the finished opening range.
        if local <= opening + dt.timedelta(minutes=minutes) or local.second or local.microsecond:
            return "OPENING_RANGE_OR_TRIGGER_BAR_NOT_CLOSED"
    elif kind in ("FIRST_GREEN_5m", "FIRST_RED_5m"):
        if (local - opening).total_seconds() < 300 or local.minute % 5 or local.second or local.microsecond:
            return "REVERSAL_BAR_NOT_CLOSED"
    elif kind == "VWAP_FAILURE":
        if (local - opening).total_seconds() < 180:
            return "VWAP_SEQUENCE_NOT_COMPLETED"
    else:
        return "UNKNOWN_TRIGGER_TYPE"
    stop_at = ev.timestamp(stop.get("as_of")) if isinstance(stop, dict) else None
    if price(stop) is None or stop_at is None or stop_at > at:
        return "STOP_NOT_KNOWN_AT_TRIGGER"
    return None


def setup_component(records, now, variant):
    record, issues = latest(records, now, "QULLAMAGGIE")
    row = copy.deepcopy(record["row"]) if record else {}
    status = row.get("setup_status", "INSUFFICIENT_DATA")
    if status not in STATUSES:
        status = "INSUFFICIENT_DATA"
        issues.append("UNKNOWN_SETUP_STATUS")
    if record:
        data_at = record.get("data_at", record["at"])
        daily = daily_close(row.get("daily_as_of"))
        if daily is None or daily > data_at:
            issues.append("DAILY_CLOSE_NOT_AVAILABLE_AT_SNAPSHOT")
        elif daily != last_completed(data_at):
            issues.append("STALE_DAILY_DATA")
        if row.get("data_error"):
            issues.append("SOURCE_DATA_ERROR")
        for key in ("invalidated_at",):
            if row.get(key) and (ev.timestamp(row[key]) is None or ev.timestamp(row[key]) > data_at):
                issues.append("FUTURE_OR_UNKNOWN_" + key.upper())
        if status == "TRIGGERED":
            issue = trigger_issue(row.get("trigger"), row.get("stop"), data_at)
            if issue:
                issues.append(issue)
            elif trigger_issue(row.get("trigger"), row.get("stop"), now):
                issues.append("NO_CURRENT_REGULAR_SESSION_TRIGGER")
            kind = str((row.get("trigger") or {}).get("type", ""))
            allowed = (kind.startswith("ORL_") or kind in ("FIRST_RED_5m", "VWAP_FAILURE")) if variant == "PARABOLIC_SHORT" else (
                kind.startswith("ORH_") or (variant == "PARABOLIC_LONG" and kind == "FIRST_GREEN_5m"))
            if not allowed:
                issues.append("TRIGGER_TYPE_DOES_NOT_MATCH_SETUP_DIRECTION")
        if variant == "EP" and status in ("SETUP_READY", "TRIGGERED"):
            cutoff = ev.timestamp((row.get("trigger") or {}).get("at")) if status == "TRIGGERED" else data_at
            catalysts = row.get("catalyst") or []
            confirmed = []
            for catalyst in ev.objects(catalysts, "EP.catalyst"):
                published, available = ev.timestamp(catalyst.get("published_at")), ev.timestamp(catalyst.get("available_at"))
                if (published and available and cutoff and max(published, available) <= cutoff and catalyst.get("reference")
                        and (data_at - published).total_seconds() <= ev.HORIZONS["EP"]["max_age_seconds"]):
                    confirmed.append(catalyst)
            if not confirmed:
                issues.append("EP_WITHOUT_AVAILABLE_CONFIRMED_CATALYST")
    if issues:
        status = "INSUFFICIENT_DATA"
    return {"setup": variant, "status": status, "reported_status": row.get("setup_status"),
            "as_of": ev.iso(record["at"]) if record else None, "issues": issues,
            "side": "SHORT" if variant == "PARABOLIC_SHORT" else "LONG",
            "trigger": row.get("trigger") if status == "TRIGGERED" else None,
            "stop": row.get("stop") if status in ACTIVE else None,
            "target": row.get("target") if status in ACTIVE else None,
            "source": row}


def classify(trend, setup):
    variant, status = setup["setup"], setup["status"]
    if status not in ACTIVE:
        return "NO_SETUP", None
    if variant == "BREAKOUT" and trend == "PASS":
        return {"CANDIDATE": ("WATCH", "FILTER_CONFLUENCE"),
                "SETUP_READY": ("LONG_SETUP", "SETUP_CONFLUENCE"),
                "TRIGGERED": ("STRONG_LONG_CONFLUENCE", "TRIGGER_CONFLUENCE")}[status]
    if variant == "EP":
        if status == "CANDIDATE":
            return "WATCH", None
        if trend == "PASS":
            return ("STRONG_LONG_CONFLUENCE" if status == "TRIGGERED" else "LONG_SETUP"), "ADDITIONAL_TREND_CONFLUENCE"
        return "EP_ONLY", None
    if variant == "PARABOLIC_SHORT":
        if status == "TRIGGERED" and trend == "PASS":
            return "CONFLICT", None
        return ("WATCH" if status == "CANDIDATE" else "SHORT_SETUP"), None
    return "QULLAMAGGIE_ONLY", None


def risk_component(setup, records, now):
    """A scanner stop-context is NOT a full risk PASS. Bind native verdicts."""
    source = setup["source"]
    entry, stop, target = price(setup["trigger"]), price(setup["stop"]), price(setup["target"])
    if entry is None or stop is None:
        return "FAIL", ["MISSING_TRIGGER_OR_STOP; full trader-risk check required"], None
    matching = []
    for record in records:
        row = record["row"]
        if ev.direction(row.get("side")) != setup["side"] or row.get("kind") != "entry":
            continue
        if (price(row.get("entry_ref")) != entry or price(row.get("stop")) != stop
                or (target is not None and price(row.get("target")) != target)):
            continue
        if row.get("instrument_type", "stock") != source.get("instrument_type", "stock") or row.get("contract") != source.get("contract"):
            continue
        matching.append(record)
    record, issues = latest(matching, now, "RISK")
    if not record:
        return "FAIL", issues + ["No current full risk verdict for these exact levels/direction/instrument"], None
    row = record["row"]
    trigger_at = ev.timestamp(setup["trigger"].get("at"))
    if record["at"] < trigger_at:
        issues.append("RISK_PREDATES_TRIGGER")
    target = price(row.get("target"))
    if target is None or not (stop < entry < target if setup["side"] == "LONG" else target < entry < stop):
        issues.append("INVALID_OR_MISSING_TARGET")
    if (row.get("verdict") != "PASS" or not row.get("fingerprint") or row["fingerprint"] == "analysis:invalid"
            or row.get("warnings") or row.get("reasons") or record.get("warnings")):
        issues.append("EXISTING_RISK_NOT_CLEARED")
    context = source.get("risk") or {}
    if (context.get("blocks") or context.get("warning") or source.get("liquidity_pass") is False
            or (setup["setup"] == "BREAKOUT" and context.get("preferred") is False)):
        issues.append("EXISTING_SCANNER_RISK_OR_LIQUIDITY_BLOCK")
    if source.get("action") is not None and source["action"] != setup["side"]:
        issues.append("EXISTING_SCANNER_ACTION_NOT_CLEARED")
    return "FAIL" if issues else "PASS", issues, target


def combine(trend, setups):
    active = [s for s in setups if s["status"] in ACTIVE]
    ready = [s for s in active if s["status"] in ("SETUP_READY", "TRIGGERED")]
    if any(s["confluence"] == "CONFLICT" for s in active) or len({s["side"] for s in ready}) > 1:
        return "CONFLICT", None
    if not active:
        return ("WATCH" if trend == "PASS" else "NO_SETUP"), None
    # Stage is explicit, not a weighted score. Never promote two candidates.
    strongest_stage = max(STAGES[s["status"]] for s in active)
    strongest = [s for s in active if STAGES[s["status"]] == strongest_stage]
    if len({s["side"] for s in strongest}) > 1:
        return "WATCH", None
    labels = {s["confluence"] for s in strongest}
    for label in ("STRONG_LONG_CONFLUENCE", "LONG_SETUP", "EP_ONLY", "SHORT_SETUP", "QULLAMAGGIE_ONLY", "WATCH"):
        if label in labels:
            return label, strongest[0]["side"]
    return "NO_SETUP", None


def explain(trend, setups, result, components, evidence):
    active = [s for s in setups if s["status"] in ACTIVE]
    why = (["Minervini PASS: mittelfristiger Trend-/Qualitätsfilter erfüllt, allein kein Entry."] if trend == "PASS" else [])
    why += [s["setup"] + " " + s["status"] + ": separates " + s["side"] + "-Setup." for s in active]
    if any(s["confluence"] == "EP_ONLY" for s in setups):
        why.append("Bestätigter EP bleibt unabhängig von Minervini gültig.")
    confirms = [k.upper() + ": " + v for k, v in components.items() if v in ("CONFIRMED", "CONFIRMING", "CONTRARIAN")]
    confirms += [s["confluence_type"] for s in active if s["confluence_type"]]
    against = [k.upper() + ": " + v for k, v in components.items() if v in ("CONFLICTING", "UNAVAILABLE", "STALE")]
    if any(e["channel"] == "SENTIMENT" and e["stance"] == "CONFLICTING" for g in evidence["groups"] for e in g["events"]):
        against.append("Sentiment-Bias widerspricht der aktuellen Setup-Richtung; als Kontext separat prüfen.")
    against += [s["setup"] + ": " + "; ".join(s["issues"] + s["risk_reasons"]) for s in setups if s["issues"] or s["risk_reasons"]]
    if trend == "INSUFFICIENT_DATA":
        against.append("Minervini: keine ausreichenden aktuellen Trenddaten.")
    elif trend == "FAIL":
        against.append("Minervini FAIL: Trendfilter nicht erfüllt; unabhängige Qullamaggie-Setups bleiben sichtbar.")
    if result == "CONFLICT":
        against.append("Trend/Setup oder aktuelle Long-/Short-Setups widersprechen sich; keine Richtung wird bevorzugt.")
        against.append("Kurzfristige Preisaktion/Short-Trigger sind aktueller als der Trendfilter; bestehende Entry-/Risk-Logik muss entscheiden.")
    if not active:
        against.append("Kein ausreichendes aktuelles Qullamaggie-Setup.")
    next_steps = ["Abgeschlossenen aktuellen Trigger sowie Stop, Ziel, Entry-, Exit-, Budget- und vollständige Risk-Prüfung bestätigen."]
    if any(s["status"] == "CANDIDATE" for s in setups):
        next_steps.append("Candidate muss zuerst SETUP_READY erreichen; Filter-Confluence ist kein Entry.")
    if result == "CONFLICT":
        next_steps.append("Widerspruch anhand aktueller Preisaktion und bestehender Entry-/Risk-Regeln klären.")
    next_steps.append("Veröffentlichungszeit und gemeinsame Primärquellen prüfen; verspätete Congress-/13F- und Dark-Pool-Daten sind nur Kontext.")
    return {"why_interesting": why or ["Derzeit keine ausreichende Evidenz für ein Setup."],
            "what_confirms": confirms or ["Keine zusätzliche unabhängige Bestätigung verfügbar."],
            "what_opposes": against, "before_trade": next_steps,
            "source_note": "%d eindeutige bestätigende Ereignisgruppen; keine Gesamtnote oder automatische Hochstufung." % evidence["independent_confirmations"]}


def analyze(document, now=None):
    if not isinstance(document, dict):
        raise ValueError("input must be a JSON object")
    json.dumps(document, allow_nan=False)
    raw_now = now if now is not None else document.get("decision_at")
    now = ev.timestamp(raw_now) if raw_now is not None else dt.datetime.now(ev.UTC)
    if now is None:
        raise ValueError("decision_at/--now needs an ISO timestamp with explicit timezone")
    if now.year not in (2026, 2027):
        raise ValueError("verified exchange calendar available for 2026-2027 only")
    trends, raw_setups, risks, events = ev.adapt(document.get("reports", []), document.get("events", []))
    symbols = {r["symbol"] for r in trends + raw_setups + risks + events}
    requested = document.get("tickers", [])
    if not isinstance(requested, list):
        raise ValueError("tickers must be an array")
    symbols.update(ev.symbol({"symbol": s}) for s in requested)
    results = []
    for sym in sorted(symbols):
        trend, detail = trend_component([r for r in trends if r["symbol"] == sym], now)
        grouped = {}
        for record in raw_setups:
            if record["symbol"] != sym:
                continue
            variant = record["row"].get("variant")
            expected = {"qullamaggie-breakout": ("BREAKOUT",), "qullamaggie-ep": ("EP",),
                        "qullamaggie-parabolic": ("PARABOLIC_LONG", "PARABOLIC_SHORT")}[record["mode"]]
            if variant not in expected:
                raise ValueError("variant does not match Qullamaggie report mode")
            grouped.setdefault(variant, []).append(record)
        setups = [setup_component(rows, now, variant) for variant, rows in sorted(grouped.items())]
        for setup in setups:
            setup["confluence"], setup["confluence_type"] = classify(trend, setup)
            setup["risk"], setup["risk_reasons"], risk_target = risk_component(setup, [r for r in risks if r["symbol"] == sym], now)
            if risk_target is not None:
                setup["target"] = risk_target
        result, side = combine(trend, setups)
        active = [s for s in setups if s["status"] in ACTIVE]
        risk = "PASS" if active and all(s["risk"] == "PASS" for s in active) else "FAIL"
        components, evidence = ev.assess_events(events, sym, now, side)
        if result == "CONFLICT":
            trade_direction = "NO_TRADE"
        elif any(s["status"] == "TRIGGERED" for s in active):
            trade_direction = side if risk == "PASS" and side else "NO_TRADE"
        else:
            trade_direction = "WATCH" if active or trend == "PASS" else "NO_TRADE"
        # RISK FAIL never produces a trade direction, including untriggered setups.
        if risk == "FAIL":
            trade_direction = "NO_TRADE"
        row = dict(components, ticker=sym, minervini=trend, minervini_detail=detail,
                   qullamaggie_setup=[s["setup"] for s in setups] or ["NONE"],
                   qullamaggie_status={s["setup"]: s["status"] for s in setups} or {"NONE": "INSUFFICIENT_DATA"},
                   risk=risk, confluence=result, direction=trade_direction,
                   confluence_types=[s["confluence_type"] for s in setups if s["confluence_type"]],
                   trigger={s["setup"]: s["trigger"] for s in setups}, stop={s["setup"]: s["stop"] for s in setups},
                   target={s["setup"]: s["target"] for s in setups}, setups=setups, evidence=evidence,
                   trade_allowed=False, signal=None, analysis_only=True)
        row["explanation"] = explain(trend, setups, result, components, evidence)
        row["reason"] = "; ".join(row["explanation"]["why_interesting"])
        results.append(row)
    return {"created_by": "trader-confluence", "decision_at": ev.iso(now), "analysis_only": True,
            "paper": True, "trade_allowed": False, "signal": None, "horizons": copy.deepcopy(ev.HORIZONS),
            "summary": {"tickers": len(results), "minervini_pass": sum(r["minervini"] == "PASS" for r in results),
                        "breakout_candidate": sum(r["qullamaggie_status"].get("BREAKOUT") == "CANDIDATE" for r in results),
                        "breakout_setup_ready": sum(r["qullamaggie_status"].get("BREAKOUT") == "SETUP_READY" for r in results),
                        "breakout_triggered": sum(r["qullamaggie_status"].get("BREAKOUT") == "TRIGGERED" for r in results),
                        "minervini_breakout_overlap": sum(r["minervini"] == "PASS" and r["qullamaggie_status"].get("BREAKOUT") in ACTIVE for r in results),
                        "confluences": {k: sum(r["confluence"] == k for r in results) for k in (
                            "STRONG_LONG_CONFLUENCE", "LONG_SETUP", "EP_ONLY", "QULLAMAGGIE_ONLY", "SHORT_SETUP", "CONFLICT", "WATCH", "NO_SETUP")}},
            "results": results, "note": NOTE}


def render(report):
    def cell(value):
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        return re.sub(r"[\r\n\t|]+", " ", str(value if value is not None else "offen"))
    header = "Ticker | Minervini | Qullamaggie Setup | Qullamaggie Status | News | Social | Quiver | Sentiment | Risk | Confluence | Direction | Trigger | Stop | Target | Begründung"
    keys = ("ticker", "minervini", "qullamaggie_setup", "qullamaggie_status", "news", "social", "quiver", "sentiment",
            "risk", "confluence", "direction", "trigger", "stop", "target", "reason")
    lines = ["Analysezeit: " + report["decision_at"], "", header, " | ".join(["---"] * len(keys))]
    for row in report["results"]:
        lines.append(" | ".join(cell(row[k]) for k in keys))
    for row in report["results"]:
        lines.append("\n" + cell(row["ticker"]))
        for key, title in (("why_interesting", "WARUM interessant?"), ("what_confirms", "WAS bestätigt das Setup?"),
                           ("what_opposes", "WAS spricht dagegen?"), ("before_trade", "WAS muss vor einem Trade noch passieren?")):
            lines.append(title + " " + cell("; ".join(row["explanation"][key])))
        lines.append(row["explanation"]["source_note"])
    return "\n".join(lines) + "\n\n" + report["note"]


def reject_constant(value):
    raise ValueError("non-finite JSON number: " + value)


def read_json(path):
    with open(path, encoding="utf-8-sig") as stream:
        return json.load(stream, parse_constant=reject_constant)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", help="bundle with reports/events/decision_at/tickers")
    for name in ("minervini", "qullamaggie", "news", "social", "quiver", "sentiment", "risk"):
        parser.add_argument("--" + name, action="append", default=[], help="native report JSON (repeatable)")
    parser.add_argument("--now", help="decision time: ISO-8601 with explicit timezone")
    parser.add_argument("--tickers", help="additional comma-separated exact tickers")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", help="optional analysis report JSON; never writes plans/orders/watchlists")
    args = parser.parse_args(argv)
    try:
        document = read_json(args.input) if args.input else {}
        if not isinstance(document, dict):
            raise ValueError("input must be an object")
        reports = list(ev.objects(document.get("reports", []), "reports"))
        for name in ("minervini", "qullamaggie", "news", "social", "quiver", "sentiment", "risk"):
            for path in getattr(args, name):
                doc = read_json(path)
                if not isinstance(doc, dict):
                    raise ValueError("report must be an object")
                expected = "trader-scanner" if name in ("minervini", "qullamaggie") else "trader-" + name
                if doc.get("created_by", expected) != expected:
                    raise ValueError("report source does not match --" + name)
                reports.append(dict(doc, created_by=expected))
        document = dict(document, reports=reports)
        if args.tickers:
            document["tickers"] = args.tickers.split(",")
        report = analyze(document, args.now)
        if args.out:
            # The output path must not alias an input or a trading pipeline artifact.
            inputs = [args.input] + [p for name in ("minervini", "qullamaggie", "news", "social", "quiver", "sentiment", "risk") for p in getattr(args, name)]
            reserved = ("orders.json", "plan.json", "entry.json", "watchlist.json", "risk.json", "broker.json", "config.json", "portfolio.json")
            if os.path.basename(args.out).lower() in reserved or any(os.path.realpath(args.out) == os.path.realpath(p) for p in inputs if p):
                raise ValueError("--out must be a separate analysis report")
            parent = os.path.dirname(args.out)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(args.out, "w", encoding="utf-8") as stream:
                json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
                stream.write("\n")
        print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) if args.json else render(report))
    except (ValueError, TypeError, KeyError, AttributeError, OSError, OverflowError) as exc:
        print("trader-confluence: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
