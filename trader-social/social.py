#!/usr/bin/env python3
"""Offline social catalyst analysis and an explicit, official-API-only X command.

No broker imports, orders, implicit network access or stored credentials.
Python 3.8+, standard library only. See SKILL.md for the evidence contract.
"""

import argparse
import datetime as dt
import importlib.util
import json
import math
import os
import re
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
IMPACTS = ("BULLISH", "BEARISH", "MIXED", "UNKLAR")
CATALYSTS = ("Produkt", "Regulierung", "Politik", "M&A", "Auftrag", "Personal",
             "Finanzierung", "Produktion", "Lieferkette", "Makro", "Sonstiges")
SKILLS = ("trader-news", "trader-scanner", "trader-entry", "trader-exit", "trader-risk", "trader-sentiment")
HEADER = "Autor | Plattform | Zeitpunkt | Ticker | Impact | Verifikation | Kursbestätigung | LONG / SHORT / KEIN TRADE | Begründung"


class SocialError(Exception):
    pass


def local_tool(folder, name):
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("social_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def timestamp(value):
    """Require an explicit offset; never guess a post's timezone."""
    if not isinstance(value, str):
        return None
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def direction(value):
    """Use the native pipeline's buy/long and sell/short direction semantics."""
    return {"BUY": "LONG", "LONG": "LONG", "SELL": "SHORT", "SHORT": "SHORT"}.get(str(value).upper())


def read_json(path):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        raise SocialError("Cannot read JSON: %s" % path) from None


def config(path=None):
    cfg = read_json(path or os.path.join(HERE, "watchlist.json"))
    if not isinstance(cfg, dict) or cfg.get("mode") != "paper-analysis":
        raise SocialError("Only paper-analysis mode is supported")
    for key in ("max_post_age_minutes", "max_evidence_age_minutes", "min_price_change_pct",
                "min_relative_volume", "max_spread_pct"):
        if number(cfg.get(key)) is None or number(cfg[key]) <= 0:
            raise SocialError("Positive finite setting required: " + key)
        cfg[key] = number(cfg[key])
    if type(cfg.get("allow_short_analysis")) is not bool:
        raise SocialError("allow_short_analysis must be a boolean")
    for key in ("accounts", "assets", "allowed_news_hosts", "allowed_official_hosts"):
        if not isinstance(cfg.get(key), list):
            raise SocialError("List required: " + key)
    for host in cfg["allowed_news_hosts"] + cfg["allowed_official_hosts"]:
        if not isinstance(host, str) or not re.fullmatch(r"[a-z0-9.-]+", host) or forbidden_host(host):
            raise SocialError("Social websites cannot be news or official-source hosts")
    for account in cfg["accounts"]:
        if (not isinstance(account, dict) or not isinstance(account.get("account"), str)
                or account.get("platform") not in ("X", "Truth Social")
                or type(account.get("enabled")) is not bool):
            raise SocialError("Invalid watchlist account")
    for asset in cfg["assets"]:
        if (not isinstance(asset, dict) or not isinstance(asset.get("symbol"), str)
                or not asset["symbol"] or not isinstance(asset.get("aliases", []), list)):
            raise SocialError("Invalid watchlist asset")
    return cfg


def forbidden_host(host):
    return any(host == h or host.endswith("." + h)
               for h in ("x.com", "twitter.com", "truthsocial.com", "truth.social"))


def source_host(source):
    try:
        parsed = urllib.parse.urlsplit(source.get("reference", ""))
        if parsed.scheme == "https" and not parsed.username and not parsed.password and parsed.port in (None, 443):
            return parsed.hostname or ""
    except (TypeError, ValueError):
        pass
    return ""


def verification(post, cfg):
    source = post.get("source") or {}
    kind, host = source.get("kind"), source_host(source)
    direct = "INDIREKT" if kind == "news" or post.get("content_kind") == "secondary" else "DIREKT"
    if (post.get("parody") is True or post.get("satire") is True
            or source.get("reviewed") is not True or not source.get("reference")):
        return "UNVERIFIED", direct, "Quelle ungeprüft, Satire oder Parodie"
    if not post.get("author") or not post.get("account") or not (post.get("text") or post.get("summary")):
        return "UNVERIFIED", direct, "Autor, Account oder Inhalt fehlen"
    accounts = [a for a in cfg["accounts"] if a.get("enabled") is True
                and a["platform"] == post.get("platform")
                and a["account"].lower() == post["account"].lower()]
    if not accounts:
        return "UNVERIFIED", direct, "Account nicht in der aktivierten Watchlist"
    identity = source.get("account_confirmed") is True
    if kind == "news" and host in cfg["allowed_news_hosts"]:
        return "CORROBORATED", "INDIREKT", "Zulässiger geprüfter Sekundärbericht; Original nicht direkt bestätigt"
    if kind == "x_api" and post.get("platform") == "X" and host == "api.x.com" and identity:
        pinned = accounts[0].get("user_id")
        uid, pid = post.get("author_id"), post.get("id")
        path = urllib.parse.urlsplit(source["reference"]).path
        if (isinstance(uid, str) and re.fullmatch(r"[0-9]+", uid)
                and isinstance(pid, str) and re.fullmatch(r"[0-9]+", pid)
                and path in ("/2/users/%s/tweets" % uid, "/2/tweets/%s" % pid)
                and (not pinned or uid == pinned)):
            return "VERIFIED", direct, "Offizielle X API; Account-ID geprüft (kein Badge-Nachweis)"
    if (kind in ("user_screenshot", "user_text") and source.get("provided_by_user") is True
            and source.get("authenticity_checked") is True and identity):
        return "VERIFIED", direct, "Benutzerbeleg manuell geprüft; keine unabhängige Plattformbestätigung"
    if (kind == "official" and host in cfg["allowed_official_hosts"] and identity
            and source.get("confirms_post") is True):
        return "VERIFIED", direct, "Zulässige offizielle Quelle bestätigt diesen Post ausdrücklich"
    return "UNVERIFIED", direct, "Herkunft oder Authentizität nicht ausreichend bestätigt"


def affected(post, cfg):
    """Find leads without turning an author's associated ticker into a claim."""
    text = post.get("text") or post.get("summary") or ""
    found = {}
    explicit = post.get("affected_assets", [])
    for item in explicit:
        if not isinstance(item, dict) or not item.get("symbol"):
            raise SocialError("affected_assets needs objects with a symbol")
        symbol = str(item["symbol"]).upper()
        if symbol in found:
            raise SocialError("Duplicate affected asset: " + symbol)
        impact = item.get("impact", "UNKLAR")
        basis = item.get("basis", "")
        found[symbol] = dict(item, symbol=symbol, impact=impact if impact in IMPACTS and basis else "UNKLAR")
    for asset in cfg["assets"]:
        sym = asset["symbol"].upper()
        aliases = asset.get("aliases", [])
        mentioned = re.search(r"(?<![\w])\$?" + re.escape(sym) + r"(?![\w])", text)
        if mentioned or any(re.search(r"(?<!\w)" + re.escape(a) + r"(?!\w)", text, re.I) for a in aliases):
            found.setdefault(sym, dict(asset, impact="UNKLAR", basis="Texttreffer; Impact muss inhaltlich geprüft werden"))
    for sym in re.findall(r"\$([A-Z][A-Z0-9.-]{0,9})\b", text):
        found.setdefault(sym, {"symbol": sym, "kind": "Ticker", "impact": "UNKLAR", "basis": "Ungeprüfter Cashtag"})
    return list(found.values()) or [{"symbol": "UNBEKANNT", "impact": "UNKLAR", "basis": "Kein konkreter Bezug belegt"}]


def fresh(value, now, minutes, earliest=None):
    point = timestamp(value)
    return bool(point and 0 <= (now - point).total_seconds() <= minutes * 60
                and (earliest is None or point >= earliest))


def market_check(market, impact, posted, now, cfg):
    problems, metrics = [], {}
    if not isinstance(market, dict) or not posted:
        return False, metrics, ["Marktdaten oder Post-Zeitpunkt fehlen"]
    if not market.get("source") or not market.get("reference"):
        problems.append("Marktdatenquelle fehlt")
    observed = timestamp(market.get("observed_at"))
    reaction = timestamp(market.get("reaction_at"))
    before_at = timestamp(market.get("before_at"))
    if (not fresh(market.get("observed_at"), now, cfg["max_evidence_age_minutes"], posted)
            or not reaction or not observed or not posted < reaction <= observed
            or not fresh(market.get("reaction_at"), now, cfg["max_evidence_age_minutes"], posted)
            or not before_at or before_at > posted
            or (posted - before_at).total_seconds() > cfg["max_evidence_age_minutes"] * 60):
        problems.append("Kursreaktion fehlt, ist veraltet oder nicht nach dem Post")
    values = {k: number(market.get(k)) for k in
              ("price_before", "price_after", "volume", "baseline_volume", "bid", "ask")}
    window = number(market.get("volume_window_seconds"))
    baseline_window = number(market.get("baseline_window_seconds"))
    if window is None or window <= 0 or window != baseline_window:
        problems.append("Volumenfenster und Baseline nicht als gleich lang bestätigt")
    if any(v is None or v <= 0 for v in values.values()) or (values["ask"] or 0) < (values["bid"] or 0):
        problems.append("Preis, Volumen oder Spread unvollständig/ungültig")
    else:
        change = (values["price_after"] / values["price_before"] - 1) * 100
        relative = values["volume"] / values["baseline_volume"]
        spread = (values["ask"] - values["bid"]) / ((values["ask"] + values["bid"]) / 2) * 100
        metrics = {"price_change_pct": round(change, 4), "relative_volume": round(relative, 4),
                   "spread_pct": round(spread, 4)}
        aligned = ((impact == "BULLISH" and change >= cfg["min_price_change_pct"])
                   or (impact == "BEARISH" and change <= -cfg["min_price_change_pct"]))
        if not aligned:
            problems.append("Keine gerichtete Kursbestätigung")
        if relative < cfg["min_relative_volume"]:
            problems.append("Volumen bestätigt den Katalysator nicht")
        if spread > cfg["max_spread_pct"]:
            problems.append("Spread zu groß")
    if (market.get("session") != "REGULAR" or market.get("trading_open") is not True
            or market.get("halted") is not False):
        problems.append("Handelszeit oder Halt-Status nicht bestätigt")
    # Reuse the existing entry skill's exchange calendar and early-close rules.
    entry = local_tool("trader-entry", "entry")
    local = entry.to_eastern(now.replace(tzinfo=None) - now.utcoffset())
    session = entry.session(local.date())
    if not session or not session[0] <= local.time() < session[1]:
        problems.append("Börse laut trader-entry-Kalender geschlossen")
    return not problems, metrics, problems


def skill_check(reports, side, posted, now, cfg):
    """Explicit reviewed reports bind all six skills to the same asset/setup."""
    problems = []
    expected = {"trader-news": ("CLEAR",), "trader-scanner": ("CONFIRMED",),
                "trader-entry": ("TRIGGERED",), "trader-exit": ("READY",),
                "trader-risk": ("PASS",), "trader-sentiment": ("ALIGNED", "NEUTRAL")}
    for skill in SKILLS:
        report = reports.get(skill) or {}
        if (report.get("reviewed") is not True or not report.get("reference")
                or report.get("status") not in expected[skill]
                or not fresh(report.get("checked_at"), now, cfg["max_evidence_age_minutes"], posted)):
            problems.append(skill + ": aktuelle geprüfte Evidenz fehlt oder Prüfung abgelehnt")
        if skill in ("trader-scanner", "trader-entry", "trader-exit", "trader-risk") and direction(report.get("side")) != side:
            problems.append(skill + ": Setup-Richtung widerspricht dem Katalysator")
        if report.get("conflict") is True:
            problems.append(skill + ": widersprüchliche Evidenz")
    if reports.get("trader-news", {}).get("conflict") is not False:
        problems.append("News-Konflikte nicht ausdrücklich ausgeschlossen")
    sentiment = reports.get("trader-sentiment", {})
    if sentiment.get("status") == "ALIGNED" and direction(sentiment.get("side")) != side:
        problems.append("Sentiment widerspricht dem Setup")
    if "native_result" in sentiment and sentiment["native_result"].get("decision") != side:
        problems.append("trader-sentiment bestätigt den nativen Paper-Kandidaten nicht")
    directional = [reports.get(s, {}) for s in ("trader-scanner", "trader-entry", "trader-exit", "trader-risk")]
    setup_ids = [r.get("setup_id") for r in directional]
    if not all(isinstance(s, str) and s for s in setup_ids) or len(set(setup_ids)) != 1:
        problems.append("Skill-Prüfungen gehören nicht nachweislich zum selben Setup")
    entry, exit_plan, risk = directional[1:]
    price, stop, target = [number(exit_plan.get(k)) for k in ("entry", "stop", "target")]
    valid_prices = all(v is not None and v > 0 for v in (price, stop, target))
    valid_plan = valid_prices and ((side == "LONG" and stop < price < target)
                                   or (side == "SHORT" and target < price < stop))
    if (not valid_plan or number(entry.get("entry")) != price
            or any(number(risk.get(k)) != number(exit_plan.get(k)) for k in ("entry", "stop", "target"))):
        problems.append("Entry, Exit-Plan und Risk-PASS sind nicht konsistent")
    if entry.get("within_entry_window") is not True:
        problems.append("Entry-Zeitfenster nicht bestätigt")
    if side == "SHORT":
        if not cfg["allow_short_analysis"]:
            problems.append("SHORT-Analyse deaktiviert; allow_short_analysis muss explizit aktiviert sein")
    return problems


def instrument_check(evidence, symbol, side):
    """Preserve PR #2's exact CFD identity; never substitute underlying-stock quotes."""
    descriptor = evidence.get("instrument") or {}
    reports = evidence.get("skills", {})
    market = evidence.get("market") or {}
    kind = descriptor.get("instrument_type", "stock")
    sources = [market] + [reports.get(s, {}) for s in
                         ("trader-scanner", "trader-entry", "trader-exit", "trader-risk", "trader-sentiment")]
    problems = []
    if kind not in ("stock", "cfd"):
        return ["Native Pipeline unterstützt nur Aktien und verifizierte USD-Aktien-CFDs"]
    if descriptor and (descriptor.get("symbol") != symbol or direction(descriptor.get("side")) != side):
        problems.append("Instrument oder Richtung stimmt nicht mit dem Paper-Setup überein")
    if kind != "cfd":
        if any(source.get("instrument_type") == "cfd" for source in sources):
            problems.append("CFD-Evidenz ohne expliziten CFD-Instrumentdescriptor")
        return problems
    contract = descriptor.get("contract")
    if any(source.get("symbol") != symbol or source.get("instrument_type") != "cfd"
           or source.get("currency") != "USD" or source.get("contract") != contract
           or source.get("price_basis") != "mid" for source in sources):
        problems.append("CFD-Vertragsidentität, Währung oder Mid-Preisbasis nicht durchgehend bestätigt")
    if market.get("mapping_verified") is not True or market.get("same_price_basis") is not True:
        problems.append("CFD-Kursdaten sind nicht dem exakten Vertrag zugeordnet")
    risk = reports.get("trader-risk", {})
    if any(item.get("analysis_only") is not True or item.get("paper") is not True for item in (descriptor, risk)):
        problems.append("CFD erfordert explizite Paper-/Analyse-Evidenz")
    exposure = local_tool("trader-risk", "exposure")
    try:
        # Reuse all native contract, currency, leverage, margin and cost validation.
        # Risk PASS still has to reference the independently checked exact plan.
        exposure.terms(descriptor)
    except exposure.ExposureError:
        problems.append("CFD-Vertragsdaten fehlen oder verletzen die native Exposure-Prüfung")
    native = reports.get("trader-sentiment", {}).get("native_result")
    if native is not None and (native.get("extracted", {}).get("symbol") != symbol
                               or native.get("extracted", {}).get("instrument_type") != kind):
        problems.append("Nativer Sentiment-Kandidat gehört nicht zum CFD-Instrument")
    return problems


def analyze(posts, evidence, cfg, now):
    if not isinstance(posts, list) or not isinstance(evidence, dict) or not isinstance(evidence.get("tickers", {}), dict):
        raise SocialError("Expected posts list and evidence.tickers object")
    results = []
    for post in posts:
        if not isinstance(post, dict) or not isinstance(post.get("source", {}), dict):
            raise SocialError("Invalid post/source object")
        source = post.get("source") or {}
        verified, direct, basis = verification(post, cfg)
        posted = timestamp(post.get("timestamp"))
        original = timestamp(post.get("original_timestamp"))
        age_point = min(posted, original) if posted and original else posted
        age = (now - age_point).total_seconds() if age_point else None
        for asset in affected(post, cfg):
            symbol, impact = asset["symbol"], asset["impact"]
            common = []
            if verified == "UNVERIFIED":
                common.append("Social-Daten unverifiziert")
            if age is None or age < 0 or age > cfg["max_post_age_minutes"] * 60:
                common.append("Post-Zeit unbekannt, zukünftig oder Post zu alt")
            if source.get("original_time_confirmed") is not True:
                common.append("Originalzeit des Posts nicht bestätigt (Berichtszeit genügt nicht)")
            if post.get("content_kind") not in ("original", "secondary"):
                common.append("Repost, Zitat, Antwort oder unbekannte Post-Art: Original separat prüfen")
            if post.get("conflicting") is True:
                common.append("Widersprüchliche Social-Daten")
            if impact not in ("BULLISH", "BEARISH"):
                common.append("Impact unklar oder gemischt")
            if asset.get("kind") == "Sektor" or symbol == "UNBEKANNT":
                common.append("Kein konkretes handelbares Instrument bestätigt")
            ticker_evidence = evidence.get("tickers", {}).get(symbol) or {}
            if not isinstance(ticker_evidence, dict) or not isinstance(ticker_evidence.get("skills", {}), dict):
                raise SocialError("Invalid per-ticker evidence")
            confirmed, metrics, market_problems = market_check(ticker_evidence.get("market"), impact, posted, now, cfg)
            side = "LONG" if impact == "BULLISH" else "SHORT" if impact == "BEARISH" else "KEIN TRADE"
            skill_problems = skill_check(ticker_evidence.get("skills", {}), side, posted, now, cfg)
            problems = common + market_problems + skill_problems + instrument_check(ticker_evidence, symbol, side)
            decision = "KEIN TRADE" if problems else side
            results.append({
                "author": post.get("author"), "account": post.get("account"), "platform": post.get("platform"),
                "post_id": post.get("id"), "timestamp": post.get("timestamp"),
                "timezone": posted.strftime("%z") if posted else "UNBEKANNT",
                "original_timestamp": post.get("original_timestamp"), "content_kind": post.get("content_kind"),
                "text": post.get("text"), "summary": post.get("summary"),
                "ticker": symbol, "affected_asset": asset, "impact": impact,
                "instrument": ticker_evidence.get("instrument"),
                "catalyst_type": post.get("catalyst_type") if post.get("catalyst_type") in CATALYSTS else "Sonstiges",
                "directness": direct, "verification": verified, "verification_basis": basis,
                "source_reference": source.get("reference"), "reported_at": source.get("reported_at"),
                "age_seconds": age, "daytrade_relevance": "KANDIDAT" if not problems else "KEIN TRADE",
                "price_confirmation": "JA" if confirmed else "NEIN", "market_metrics": metrics,
                "decision": decision, "reasons": problems or ["Unabhängiges Setup und alle sechs Prüfungen bestätigt; nur Paper-/Analysekandidat"],
                "mode": "paper-analysis"})
    # Contradictory current posts cannot independently yield an actionable row.
    # Old posts do not contaminate a current catalyst merely by being present.
    current = {}
    for row in results:
        if row["age_seconds"] is not None and 0 <= row["age_seconds"] <= cfg["max_post_age_minutes"] * 60:
            current.setdefault(row["ticker"], set()).add(row["impact"])
    for row in results:
        impacts = current.get(row["ticker"], set())
        if {"BULLISH", "BEARISH"}.issubset(impacts) or "MIXED" in impacts:
            if row["decision"] != "KEIN TRADE":
                row["reasons"] = []
            row["decision"] = row["daytrade_relevance"] = "KEIN TRADE"
            row["reasons"].append("Widersprüchliche aktuelle Social-Posts zum selben Instrument")
    return {"created_by": "trader-social", "checked_at": now.isoformat(), "mode": "paper-analysis", "results": results}


def render(result):
    def cell(value):
        return re.sub(r"[\r\n\t|]+", " ", str(value if value is not None else "UNBEKANNT"))
    lines = [HEADER]
    for row in result["results"]:
        reason = "; ".join(row["reasons"])
        context = "Account=%s; Art=%s; Direktheit=%s; Alter=%s s; Katalysator=%s; %s" % (
            row["account"], row["content_kind"], row["directness"], row["age_seconds"],
            row["catalyst_type"], row["verification_basis"])
        lines.append(" | ".join(cell(v) for v in (row["author"], row["platform"], row["timestamp"],
                    row["ticker"], row["impact"], row["verification"], row["price_confirmation"],
                    row["decision"], reason + "; " + context)))
        lines.append("  Inhalt: %s; Quelle: %s; Impact-Bezug: %s" % (
            cell(row["text"] or row["summary"]), cell(row["source_reference"]),
            cell(row["affected_asset"].get("basis"))))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("analyze", "x"):
        sub = commands.add_parser(command)
        sub.add_argument("--watchlist")
        sub.add_argument("--json", action="store_true")
        if command == "analyze":
            sub.add_argument("--posts", required=True)
            sub.add_argument("--evidence")
            sub.add_argument("--now", help="ISO-8601 with UTC offset, for reproducible offline analysis")
        else:
            sub.add_argument("--account", default="elonmusk")
            sub.add_argument("--max-results", type=int, default=10)
    args = parser.parse_args(argv)
    try:
        cfg = config(args.watchlist)
        if args.command == "x":
            account = next((a for a in cfg["accounts"] if a["account"].lower() == args.account.lower()
                            and a["platform"] == "X" and a["enabled"] is True), None)
            if account is None:
                raise SocialError("Enabled X account required; Truth Social is input-only")
            result = local_tool("trader-social", "x_api").fetch_account(account, args.max_results)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "OK" else 1
        now = timestamp(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
        if now is None:
            raise SocialError("--now requires an explicit timezone offset")
        posts = read_json(args.posts)
        if isinstance(posts, dict):
            if posts.get("status") == "DATA UNAVAILABLE":
                unavailable = {"status": "DATA UNAVAILABLE", "decision": "KEIN TRADE", "results": []}
                print(json.dumps(unavailable) if args.json else "DATA UNAVAILABLE | KEIN TRADE")
                return 1
            posts = posts.get("posts")
        evidence = read_json(args.evidence) if args.evidence else {"tickers": {}}
        result = analyze(posts, evidence, cfg, now)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
        return 0
    except (SocialError, TypeError, AttributeError, KeyError, ValueError):
        # User files may accidentally contain secrets; do not echo raw data/errors.
        print("trader-social: Invalid input/configuration; KEIN TRADE", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
