#!/usr/bin/env python3
"""Offline Plus500 +Insights transcription validator and paper-analysis gate.

Images are read by the skill's vision workflow, not by this program. Input is
an explicit JSON transcription with optional evidence from the existing skills.
Standard library, Python 3.8+, no network, no writes, no broker imports.
"""

import argparse
import datetime as dt
import importlib.util
import json
import math
import os
import re
import sys
from decimal import Decimal, InvalidOperation

UTC = dt.timezone.utc
HIGH = 70
EXTREME = 85
MAX_SPREAD_PCT = 0.5
MAX_SPREAD_RISK = 0.20
FIELDS = ("buy_pct", "sell_pct", "daily_change_pct", "buy_price", "sell_price", "spread", "leverage")
LONG_SETUPS = {"breakout_20d", "bull_flag", "reclaim_ma20", "oversold_bounce", "orb"}


class SentimentError(ValueError):
    pass


def skill(folder, filename):
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), folder, filename + ".py")
    spec = importlib.util.spec_from_file_location("sentiment_" + filename, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def timestamp(value):
    try:
        result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timezone missing")
        return result.astimezone(UTC)
    except (TypeError, ValueError):
        raise SentimentError("Zeitpunkt fehlt oder hat keine eindeutige Zeitzone")


def fresh(value, now, minutes, label, reasons):
    try:
        age = (now - timestamp(value)).total_seconds()
        if age < 0 or age > minutes * 60:
            reasons.append(label + ": veraltet oder in der Zukunft")
    except SentimentError:
        reasons.append(label + ": Zeitpunkt/Zeitzone fehlt")


def number(value, locale="de-DE", leverage=False):
    """Never guess a decimal separator or turn a missing value into zero."""
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "?", "-", "n/a", "unleserlich")):
        return None
    if isinstance(value, bool):
        raise SentimentError("boolescher Wert statt Zahl")
    if isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip().replace("\u2212", "-").replace("\u00a0", " ")
        if leverage:
            ratio = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*", text)
            if ratio:
                a, b = (Decimal(v) for v in ratio.groups())
                if a <= 0 or b <= 0 or (a != 1 and b != 1):
                    raise SentimentError("Hebelverhaeltnis unklar")
                result = float(max(a, b))
                if not math.isfinite(result):
                    raise SentimentError("Hebel ist nicht endlich")
                return result
            text = re.sub(r"^[xX\u00d7]|[xX\u00d7]$", "", text).strip()
        text = re.sub(r"^(?:USD|EUR|GBP|[$\u20ac\u00a3])\s*|\s*(?:USD|EUR|GBP|[$\u20ac\u00a3]|%)$", "", text).strip()
        decimal, grouping = (",", ".") if locale == "de-DE" else (".", ",")
        pattern = r"[+-]?(?:\d+|\d{1,3}(?:%s\d{3})+)(?:%s\d+)?" % (re.escape(grouping), re.escape(decimal))
        if not re.fullmatch(pattern, text):
            raise SentimentError("Zahlenformat unklar: %r" % value)
        text = text.replace(grouping, "").replace(decimal, ".")
    else:
        raise SentimentError("kein Zahlenwert")
    try:
        result = float(Decimal(text))
    except InvalidOperation:
        raise SentimentError("ungueltige Zahl")
    if not math.isfinite(result):
        raise SentimentError("Zahl ist nicht endlich")
    return result


def normalize(row, screenshot):
    locale = screenshot.get("locale")
    if locale not in ("de-DE", "en-US"):
        raise SentimentError("Screenshot-locale muss de-DE oder en-US sein")
    if not isinstance(row, dict):
        raise SentimentError("Kandidat muss ein JSON-Objekt sein")
    result = {key: row.get(key) for key in ("instrument", "symbol", "instrument_type", "currency")}
    result.update(sentiment_period=row.get("sentiment_period") or screenshot.get("sentiment_period"),
                  category=row.get("category") or screenshot.get("category"), raw={k: row.get(k) for k in FIELDS})
    errors = []
    for field in FIELDS:
        try:
            result[field] = number(row.get(field), locale, field == "leverage")
        except SentimentError as exc:
            result[field] = None
            errors.append(field + ": " + str(exc))
    for field in ("buy_pct", "sell_pct"):
        if result[field] is not None and not 0 <= result[field] <= 100:
            errors.append(field + ": muss zwischen 0 und 100 liegen")
    if all(result[f] is not None for f in ("buy_pct", "sell_pct")):
        if abs(result["buy_pct"] + result["sell_pct"] - 100) > 1:
            errors.append("Kaufquote und Verkaufsquote sind widerspruechlich")
    for field in ("buy_price", "sell_price", "leverage"):
        if result[field] is not None and result[field] <= 0:
            errors.append(field + ": muss positiv sein")
    reported = result["spread"]
    result["spread_reported"] = reported
    result["spread_derived"] = None
    result["spread_pct"] = None
    ask, bid = result["buy_price"], result["sell_price"]
    if reported is not None and (reported < 0 or row.get("spread_unit") != "price"):
        errors.append("Spread negativ oder Einheit unklar (price erforderlich)")
    if ask is not None and bid is not None and ask > 0 and bid > 0:
        derived = float(Decimal(str(ask)) - Decimal(str(bid)))
        result["spread_derived"] = derived
        if derived < 0:
            errors.append("Kaufkurs liegt unter Verkaufskurs; Spalten pruefen")
        if reported is not None and abs(reported - derived) > 0.000001:
            errors.append("Angezeigter Spread passt nicht zu Kaufkurs minus Verkaufskurs")
        if reported is None:
            result["spread"] = derived
        result["spread_pct"] = derived / ((ask + bid) / 2) * 100
    result["missing_fields"] = [k for k in FIELDS if k != "spread" and result[k] is None]
    result["errors"] = errors
    return result


def sentiment_bias(buy, sell, change, closes):
    """A possible interpretation, never an entry or a reversal forecast."""
    if buy is None or sell is None or change is None or len(closes) < 3:
        return None, "Unzureichende Daten zum Vergleich von Sentiment und Kurs"
    up = closes[-3] < closes[-2] < closes[-1] and change > 0
    down = closes[-3] > closes[-2] > closes[-1] and change < 0
    if buy >= HIGH and up:
        return "LONG", "Retail-Kaufmehrheit stimmt mit steigenden Kursen ueberein"
    if sell >= HIGH and down:
        return "SHORT", "Retail-Verkaufsmehrheit stimmt mit fallenden Kursen ueberein"
    if buy >= EXTREME and down:
        return "SHORT", "Moegliche kontraere/bearishe Divergenz: extreme Kaufquote bei weiter fallenden Kursen"
    if sell >= EXTREME and up:
        return "LONG", "Moegliche kontraere/bullishe Divergenz: extreme Verkaufsquote bei weiter steigenden Kursen"
    return None, "Keine eindeutige Uebereinstimmung oder bestaetigte extreme Divergenz"


def evaluate(row, screenshot, now):
    extracted = normalize(row, screenshot)
    reasons = list(extracted["errors"])
    result = {"instrument": extracted.get("instrument") or "unbekannt", "decision": "KEIN TRADE",
              "quality": "nicht ausreichend bestaetigt", "trigger": None, "stop": None, "target": None,
              "bias": None, "extracted": extracted, "reasons": reasons, "risk": None}
    if extracted["missing_fields"]:
        reasons.append("Screenshot-Felder fehlen: " + ", ".join(extracted["missing_fields"]))
    if not extracted["instrument"] or not extracted["symbol"] or not extracted["sentiment_period"]:
        reasons.append("Instrument, eindeutiger Ticker oder Sentiment-Zeitraum fehlt")
    if extracted["category"] not in ("highest_buy", "highest_sell"):
        reasons.append("Insights-Kategorie fehlt oder ist unbekannt")
    if not screenshot.get("file"):
        reasons.append("Screenshot-Referenz fehlt")
    fresh(screenshot.get("captured_at"), now, 15, "Screenshot", reasons)
    fresh(screenshot.get("captured_at"), now, 5, "Screenshot-Kauf-/Verkaufskurse", reasons)
    context = row.get("context") or {}
    market = context.get("market") or {}
    symbol = extracted["symbol"]
    if market.get("symbol") != symbol or market.get("mapping_verified") is not True:
        reasons.append("Zuordnung von Plus500-Instrument und Kursdaten nicht bestaetigt")
    if market.get("same_price_basis") is not True or market.get("currency") != extracted["currency"]:
        reasons.append("Preis-/Waehrungsbasis stimmt nicht nachweislich ueberein")
    fresh(market.get("as_of"), now, 5, "Kursdaten", reasons)
    if not market.get("source"):
        reasons.append("Quelle der Kursbewegung fehlt")
    if market.get("sentiment_period") != extracted["sentiment_period"] or not extracted["sentiment_period"] or market.get("period_verified") is not True:
        reasons.append("Kursbewegung und Sentiment haben keinen vergleichbaren Zeitraum")
    bars = market.get("bars") or []
    closes = [number(bar.get("close"), "en-US") for bar in bars]
    if any(v is None or v <= 0 for v in closes):
        raise SentimentError("Kursbalken muessen positive Schlusskurse enthalten")
    bar_times = [timestamp(bar.get("close_time")) for bar in bars]
    bar_step = {"1m": 60, "5m": 300}.get(market.get("interval"))
    if len(bars) < 3 or not bar_step or any(t > now or t.second or t.microsecond or (bar_step and t.minute % (bar_step // 60)) for t in bar_times) or any((b - a).total_seconds() != bar_step for a, b in zip(bar_times, bar_times[1:])):
        reasons.append("Mindestens drei lueckenlose abgeschlossene 1m-/5m-Kursbalken erforderlich")
    if bar_times:
        fresh(bar_times[-1].isoformat(), now, 5, "Letzter abgeschlossener Kursbalken", reasons)
        if timestamp(market.get("as_of")) < bar_times[-1]:
            reasons.append("Kursbalken liegt nach dem angegebenen Datenstand")
    change = number(market.get("period_change_pct"), "en-US")
    bias, why = sentiment_bias(extracted["buy_pct"], extracted["sell_pct"], change, closes)
    result["bias"] = bias
    result["sentiment_interpretation"] = why
    if not bias:
        reasons.append(why)
    # CFD prices, contract sizes, margin and shorts have no end-to-end support
    # in the existing equity entry/exit/risk pipeline. Never relabel them.
    if extracted["instrument_type"] != "stock" or market.get("instrument_type") != "stock" or extracted["currency"] != "USD" or extracted["leverage"] != 1:
        reasons.append("Instrument nicht unterstuetzt: bestehende Pipeline prueft nur ungehebelte USD-Aktien, keine CFDs")
    if bias == "SHORT":
        reasons.append("Short-Richtung nur Analyse: trader-entry und trader-exit unterstuetzen derzeit nur Long")
    scan = skill("trader-scanner", "scan")
    local = scan.to_eastern(now.replace(tzinfo=None))
    session = scan.session(local.date())
    if local.year not in scan.CALENDAR_YEARS:
        reasons.append("Handelskalender fuer dieses Jahr ist nicht verifiziert")
    if not session or not session[0] <= local.time() < session[1]:
        reasons.append("Ausserhalb der regulaeren US-Aktienhandelszeit")
    scanner = context.get("scanner") or {}
    fresh(scanner.get("created_at"), now, 15, "trader-scanner", reasons)
    hits = [h for h in scanner.get("hits", []) if h.get("symbol") == symbol]
    tags = {tag for hit in hits for tag in hit.get("setups", [])}
    if scanner.get("created_by") != "trader-scanner" or not tags.intersection(LONG_SETUPS):
        reasons.append("Kein bestaetigtes Kursstruktur-Setup von trader-scanner (oversold allein reicht nicht)")
    expected = local.date()
    if session is None or local.time() < session[1]:
        expected -= dt.timedelta(days=1)
    while scan.session(expected) is None:
        expected -= dt.timedelta(days=1)
    if scanner.get("mode") == "daily":
        if not hits or any(h.get("close_date") != expected.isoformat() for h in hits):
            reasons.append("Scanner-Schlusskurse sind nicht vom juengsten abgeschlossenen Handelstag")
    elif scanner.get("mode") == "orb":
        if not str(scanner.get("as_of", "")).startswith(local.date().isoformat()):
            reasons.append("ORB-Scan ist nicht von heute")
        try:
            cutoff = dt.datetime.strptime(scanner["as_of"], "%Y-%m-%d %H:%M ET")
            fresh(scan.eastern_to_utc(cutoff).replace(tzinfo=UTC).isoformat(), now, 15, "ORB-Datenstand", reasons)
        except (KeyError, TypeError, ValueError):
            reasons.append("ORB-Datenstand ist ungueltig")
    else:
        reasons.append("Scanner-Modus fehlt oder ist unbekannt")
    news = context.get("news") or {}
    fresh(news.get("checked"), now, 60, "trader-news", reasons)
    review = context.get("news_review") or {}
    if "items" not in news or news.get("notes") or review.get("symbol") != symbol or review.get("reviewed") is not True or review.get("blocking_catalyst") is not False or not review.get("reason"):
        reasons.append("Nachrichten/Katalysatoren fehlen, sind ungeprueft, blockierend oder teilweise nicht erreichbar")
    entry = context.get("entry") or {}
    trigger = entry.get("trigger") or {}
    level = number(entry.get("level"), "en-US")
    if level is not None:
        result["trigger"] = "Abgeschlossener %s-Kursbalken ueber %.4f" % (entry.get("interval", "?"), level)
    if entry.get("symbol") != symbol or entry.get("side") != "buy" or entry.get("status") != "triggered" or entry.get("phase") != "open" or entry.get("date") != local.date().isoformat():
        reasons.append("trader-entry bestaetigt keinen aktuellen Long-Trigger waehrend der Sitzung")
    interval = {"1m": 1, "5m": 5}.get(entry.get("interval"))
    age = number(entry.get("minutes_since"), "en-US")
    if not interval or age is None or age < 0 or age > interval:
        reasons.append("Entry-Trigger fehlt oder ist mehr als einen Kursbalken alt")
    checked_local = None
    try:
        checked_local = dt.datetime.strptime(entry["now"], "%Y-%m-%d %H:%M:%S")
        fresh(scan.eastern_to_utc(checked_local).replace(tzinfo=UTC).isoformat(), now, interval or 5, "trader-entry", reasons)
    except (KeyError, TypeError, ValueError):
        reasons.append("Zeitpunkt der Entry-Pruefung fehlt")
    if trigger.get("close_time"):
        try:
            clock = dt.datetime.strptime(trigger["close_time"], "%H:%M").time()
            closed = dt.datetime.combine(local.date(), clock)
            elapsed = (local - closed).total_seconds() / 60
            checked_age = int((checked_local - closed).total_seconds() // 60) if checked_local else None
            if elapsed < 0 or not interval or elapsed > interval or age != checked_age:
                reasons.append("Entry-Triggerzeit passt nicht zum aktuellen Kursbalken")
        except ValueError:
            reasons.append("Entry-Triggerzeit ist ungueltig")
    else:
        reasons.append("Abschlusszeit des Entry-Triggers fehlt")
    if entry.get("data_note"):
        reasons.append("trader-entry meldet unbestaetigte/verzoegerte Triggerdaten")
    exit_plan = context.get("exit") or {}
    fresh(exit_plan.get("created_at"), now, 15, "trader-exit", reasons)
    plan_entry = number(exit_plan.get("entry"), "en-US")
    stop = number(exit_plan.get("stop"), "en-US")
    target = number(exit_plan.get("target"), "en-US")
    entry_price = number(trigger.get("price"), "en-US")
    if exit_plan.get("created_by") != "trader-exit" or exit_plan.get("symbol") != symbol or exit_plan.get("side") != "buy" or exit_plan.get("date") != local.date().isoformat() or plan_entry != entry_price or plan_entry is None or stop is None or target is None or not 0 < stop < plan_entry < target:
        reasons.append("Kein passender trader-exit-Plan mit gueltigem Stop und Ziel")
    spread = extracted["spread_derived"]
    if spread is None or spread < 0 or extracted["spread_pct"] > MAX_SPREAD_PCT:
        reasons.append("Spread fehlt, ist ungueltig oder groesser als 0,5 Prozent")
    if spread is not None and plan_entry is not None and stop is not None and spread > (plan_entry - stop) * MAX_SPREAD_RISK:
        reasons.append("Spread verbraucht mehr als 20 Prozent des geplanten Stop-Abstands")
    risk_input = context.get("risk_inputs") or {}
    fresh(risk_input.get("as_of"), now, 15, "Risikodaten", reasons)
    cfg = risk_input.get("config") or {}
    if cfg.get("paper") is not True or cfg.get("require_stop") is not True:
        reasons.append("Explizite Paper-Konfiguration mit verpflichtendem Stop fehlt")
    if not isinstance(risk_input.get("portfolio"), dict) or not isinstance(risk_input.get("state"), dict) or "orders" not in risk_input.get("state", {}):
        reasons.append("Expliziter Paper-Kontostand oder Tagesrisikostand fehlt")
    qty = number(risk_input.get("qty"), "en-US")
    if qty is None or qty <= 0 or qty != int(qty):
        reasons.append("Gepruefte ganze Aktien-Stueckzahl fehlt")
    if not reasons and bias == "LONG":
        risk = skill("trader-risk", "check")
        # An ephemeral proposal for the pure checker. No order file, ticket,
        # persisted verdict or connector is created or called.
        proposal = {"symbol": symbol, "side": "buy", "qty": str(int(qty)), "type": "market", "time_in_force": "day",
                    "order_class": "bracket", "stop_loss_stop_price": str(stop), "take_profit_limit_price": str(target),
                    "plan": {"entry": plan_entry, "stop": stop, "target": target}}
        try:
            checked = risk.check([proposal], cfg, risk_input["portfolio"],
                                 {symbol: extracted["buy_price"]}, risk_input["state"],
                                 now=now.replace(tzinfo=None), price_times={symbol: timestamp(market["as_of"]).replace(tzinfo=None)})
        except risk.RiskError as exc:
            reasons.append("trader-risk: " + str(exc))
            return result
        result["risk"] = checked
        verdict = checked["results"][0]
        if verdict["verdict"] != "PASS" or verdict["kind"] != "entry" or checked["warnings"] or verdict["warnings"]:
            reasons.extend(verdict["reasons"] + verdict["warnings"] + checked["warnings"])
            reasons.append("trader-risk hat den exakten Paper-Plan nicht ohne offene Hinweise bestaetigt")
        else:
            result.update(decision="LONG", quality="mittel (kontraer bestaetigt)" if "kontraer" in why else "hoch (Regeln erfuellt)", stop=stop, target=target)
    if result["decision"] == "LONG":
        reasons.append(why + "; Kursstruktur, Nachrichten, Entry, Exit und trader-risk bestaetigt")
    elif bias:
        reasons.insert(0, why + "; nur Sentiment-Bias " + bias + ", keine Trade-Bestaetigung")
    return result


def analyze(document, now=None, extract_only=False):
    if not isinstance(document, dict) or document.get("source") != "Plus500 +Insights":
        raise SentimentError("source muss Plus500 +Insights sein")
    screenshot = document.get("screenshot")
    rows = document.get("candidates")
    if not isinstance(screenshot, dict) or not isinstance(rows, list) or not rows:
        raise SentimentError("screenshot und eine nichtleere candidates-Liste erforderlich")
    now = timestamp(now) if now else dt.datetime.now(UTC)
    results = []
    for row in rows:
        try:
            results.append(normalize(row, screenshot) if extract_only else evaluate(row, screenshot, now))
        except (SentimentError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            results.append({"instrument": row.get("instrument", "unbekannt") if isinstance(row, dict) else "unbekannt",
                            "decision": "KEIN TRADE", "quality": "ungueltige/unvollstaendige Daten", "trigger": None,
                            "stop": None, "target": None, "bias": None, "reasons": [str(exc)]})
    return {"created_by": "trader-sentiment", "mode": "analysis-only", "paper": True,
            "as_of": now.isoformat(), "source": document["source"], "screenshot": screenshot,
            "extract_only": extract_only, "candidates": results,
            "note": "Regelbasierte Paper-Analyse, keine Handelsempfehlung. Keine Orders, keine Brokerverbindung."}


def render(report):
    def cell(value):
        return str(value if value is not None else "offen").replace("|", "/").replace("\n", " ").replace("\r", " ")
    screenshot = report.get("screenshot", {})
    lines = ["Paper-/Analysemodus. Quelle: %s. Aufnahmezeit: %s. Sentiment-Zeitraum: %s." % (
                 cell(report.get("source")), cell(screenshot.get("captured_at")), cell(screenshot.get("sentiment_period"))), "",
             "Instrument | LONG / SHORT / KEIN TRADE | Qualitaet des Setups | moeglicher Trigger | Stop | Ziel | Begruendung",
             "--- | --- | --- | --- | --- | --- | ---"]
    for row in report["candidates"]:
        lines.append(" | ".join(cell(v) for v in (row["instrument"], row["decision"], row["quality"], row["trigger"],
                                                  row["stop"], row["target"], "; ".join(row["reasons"]))))
    return "\n".join(lines) + "\n\n" + report["note"] + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="JSON-Transkription, kein Bild")
    parser.add_argument("--now", help="Analysezeit mit Zeitzone, nur fuer einen expliziten Offline-Replay setzen")
    parser.add_argument("--extract-only", action="store_true", help="nur normalisierte Screenshot-Felder anzeigen")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        with open(args.input, encoding="utf-8") as handle:
            document = json.load(handle, parse_constant=reject_constant)
        report = analyze(document, args.now, args.extract_only)
    except (OSError, ValueError) as exc:
        print("sentiment: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) if args.json or args.extract_only else render(report), end="\n")
    return 0


def reject_constant(value):
    raise SentimentError("Nicht-endlicher JSON-Zahlenwert: " + value)


if __name__ == "__main__":
    sys.exit(main())
