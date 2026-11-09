"""Offline adapters, availability checks and primary-event grouping (Python 3.8+)."""

import datetime as dt
import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

UTC = dt.timezone.utc
CHANNELS = ("NEWS", "SOCIAL", "QUIVER", "SENTIMENT")
HORIZONS = {
    "MINERVINI": {"max_age_seconds": 7 * 86400, "role": "medium-term trend/quality filter; never an entry"},
    "QULLAMAGGIE": {"max_age_seconds": 15 * 60, "role": "short-term setup/timing snapshot"},
    "EP": {"max_age_seconds": 96 * 3600, "role": "event/repricing catalyst; current price/volume still required"},
    "NEWS": {"max_age_seconds": 24 * 3600, "role": "time-sensitive catalyst context"},
    "SOCIAL": {"max_age_seconds": 4 * 3600, "role": "time-sensitive primary post context"},
    "QUIVER": {"max_age_seconds": 7 * 86400, "role": "low short-term relevance: delayed disclosures; activity is context only"},
    "SENTIMENT": {"max_age_seconds": 15 * 60, "role": "positioning context; no independent entry"},
    "RISK": {"max_age_seconds": 15 * 60, "role": "existing full risk verdict for the exact instrument/side/levels"},
}


def timestamp(value):
    try:
        point = dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if not isinstance(point, dt.datetime) or point.tzinfo is None or point.utcoffset() is None:
            return None
        return point.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        return None


def iso(point):
    return point.isoformat().replace("+00:00", "Z") if point else None


def objects(value, label):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(label + " must be an object array")
    return value


def symbol(row):
    value = row.get("symbol") or row.get("ticker")
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9^][A-Za-z0-9.^_:/-]{0,63}", value):
        raise ValueError("each record needs an exact symbol/ticker")
    return value.upper()


def direction(value):
    return {"buy": "LONG", "long": "LONG", "bullish": "LONG", "sell": "SHORT",
            "short": "SHORT", "bearish": "SHORT"}.get(str(value).lower())


def snapshot(doc):
    points = []
    for key in ("created_at", "as_of", "checked_at", "checked"):
        if key not in doc:
            continue
        # Native Minervini labels its completed daily session separately.
        if key == "as_of" and doc.get("mode") == "minervini" and re.fullmatch(r"\d{4}-\d{2}-\d{2} close", str(doc[key])):
            continue
        point = timestamp(doc[key])
        if point is None:
            return None
        points.append(point)
    return max(points) if points else None


def temporal(point, now, channel):
    if point is None:
        return "UNKNOWN_AVAILABILITY"
    age = (now - point).total_seconds()
    if age < 0:
        return "NOT_YET_AVAILABLE"
    if age > HORIZONS[channel]["max_age_seconds"]:
        return "STALE"
    return None


def url_key(value):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    parts = urlsplit(value)
    if not parts.hostname:
        return value
    host = parts.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    post = re.search(r"/status/(\d+)", parts.path)
    if host in ("x.com", "twitter.com", "mobile.twitter.com") and post:
        return "x.com/i/status/" + post.group(1)
    query = sorted((k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")
                   and k.lower() not in ("ref", "fbclid", "gclid"))
    return urlunsplit(("https", host + ((":" + str(parts.port)) if parts.port else ""),
                       parts.path.rstrip("/"), urlencode(query), ""))


def group_events(events):
    """Transitive source/event deduplication; no text-similarity guesses."""
    groups = []
    for index, event in enumerate(events):
        keys = set()
        if event.get("event_id"):
            keys.add("event:" + str(event["event_id"]))
        for key in ("primary_source", "reference"):
            ref = url_key(event.get(key))
            if ref:
                keys.add("source:" + ref)
        if not keys:
            keys.add("unidentified:" + str(index))
        touching = [g for g in groups if g["keys"] & keys]
        merged = {"keys": set(keys), "events": [event]}
        for group in touching:
            merged["keys"].update(group["keys"])
            merged["events"].extend(group["events"])
            groups.remove(group)
        groups.append(merged)
    result = []
    for group in groups:
        key = "|".join(sorted(group["keys"]))
        result.append({"id": hashlib.sha256(key.encode("utf-8")).hexdigest()[:12],
                       "identities": sorted(group["keys"]), "events": group["events"]})
    return sorted(result, key=lambda g: g["id"])


def event(row, channel, published, observed, reference, **extra):
    out = dict(row, channel=channel, symbol=symbol(row), published_at=published,
               available_at=observed, reference=reference)
    out.update(extra)
    return out


def adapt(reports, reviewed_events):
    """Keep native facts neutral unless the source actually verified direction."""
    trend, setups, risks, events = [], [], [], []
    for doc in objects(reports, "reports"):
        source, mode, point = doc.get("created_by"), doc.get("mode"), snapshot(doc)
        if source == "trader-scanner" and mode in ("minervini", "qullamaggie-breakout", "qullamaggie-ep", "qullamaggie-parabolic"):
            for row in objects(doc.get("results", []), "scanner.results"):
                record = {"symbol": symbol(row), "row": row, "at": point, "mode": mode,
                          "data_at": timestamp(doc.get("as_of")) or point}
                (trend if mode == "minervini" else setups).append(record)
        elif source == "trader-news" or (source is None and "items" in doc and "checked" in doc):
            for row in objects(doc.get("items", []), "news.items"):
                touched = row.get("touches", [])
                if not isinstance(touched, list):
                    raise ValueError("news.touches must be a ticker array")
                for ticker in sorted(set(touched + [symbol(row)])):
                    events.append(event(dict(row, symbol=ticker), "NEWS", row.get("time"), iso(point), row.get("link"), confirmed=False))
        elif source == "trader-social":
            for row in objects(doc.get("results", []), "social.results"):
                verified = row.get("verification") in ("VERIFIED", "CORROBORATED")
                events.append(event(row, "SOCIAL", row.get("original_timestamp"), iso(point), row.get("source_reference"),
                                    confirmed=verified and row.get("price_confirmation") == "JA",
                                    direction=direction(row.get("impact")),
                                    conflicting=verified and (row.get("impact") == "MIXED" or any("Widersprüchliche aktuelle" in str(r) for r in row.get("reasons", [])))))
        elif source == "trader-quiver":
            for row in objects(doc.get("records", []), "quiver.records"):
                events.append(event(row, "QUIVER", row.get("disclosure_timestamp"), iso(point), row.get("source_reference"),
                                    confirmed=False, context_only=True))
        elif source == "trader-sentiment":
            for row in objects(doc.get("candidates", []), "sentiment.candidates"):
                extracted = row.get("extracted") or row
                if not extracted.get("symbol"):
                    continue
                screenshot = doc.get("screenshot") or {}
                # A mere screenshot extraction has no price-confirmed bias.
                confirmed = not doc.get("extract_only") and row.get("decision") in ("LONG", "SHORT")
                events.append(event(dict(row, symbol=extracted["symbol"]), "SENTIMENT", screenshot.get("captured_at"),
                                    iso(point), screenshot.get("file"), confirmed=confirmed,
                                    direction=direction(row.get("bias")),
                                    assessment="CONTRARIAN" if "kontraer" in str(row.get("quality", "")).lower() else "CONFIRMING"))
        elif source == "trader-risk" or (source is None and "results" in doc and "now_new_york" in doc):
            for row in objects(doc.get("results", []), "risk.results"):
                risks.append({"symbol": symbol(row), "row": row, "at": point, "warnings": doc.get("warnings", [])})
        else:
            raise ValueError("unsupported report source/mode: %s/%s" % (source, mode))
    for row in objects(reviewed_events, "events"):
        channel = str(row.get("source_skill", "")).replace("trader-", "").upper()
        if channel not in CHANNELS:
            raise ValueError("events.source_skill must identify news/social/quiver/sentiment")
        publication_key = {"NEWS": "published_at", "SOCIAL": "posted_at", "QUIVER": "disclosure_timestamp", "SENTIMENT": "captured_at"}[channel]
        publication = row.get(publication_key)
        events.append(event(row, channel, publication, row.get("available_at"), row.get("reference"),
                            direction=direction(row.get("direction")),
                            context_only=channel == "QUIVER" and str(row.get("dataset", "")).lower() in ("congress", "13f", "darkpool", "dark-pool")))
    return trend, setups, risks, events


def assess_events(events, symbol_value, now, side):
    accepted, excluded = [], []
    for item in events:
        if item["symbol"] != symbol_value:
            continue
        row = dict(item)
        published, observed = timestamp(row.get("published_at")), timestamp(row.get("available_at"))
        issue = temporal(max(published, observed) if published and observed else None, now, row["channel"])
        if not issue and (now - published).total_seconds() > HORIZONS[row["channel"]]["max_age_seconds"]:
            issue = "STALE"
        if not row.get("reference"):
            issue = issue or "MISSING_REFERENCE"
        if row.get("quality") in ("INCONSISTENT", "INCOMPLETE"):
            issue = issue or "INVALID_SOURCE_DATA"
        if issue:
            excluded.append(dict(row, exclusion=issue))
            continue
        stance = "NEUTRAL"
        if row.get("confirmed") is True and not row.get("context_only"):
            if row.get("conflicting") is True or (side and row.get("direction") and row["direction"] != side):
                stance = "CONFLICTING"
            elif side and row.get("direction") == side:
                stance = "CONFIRMED"
        row.update(stance=stance, effective_at=iso(max(published, observed)),
                   age_seconds=(now - published).total_seconds())
        accepted.append(row)
    groups = group_events(accepted)
    components = {}
    for channel in CHANNELS:
        rows = [e for e in accepted if e["channel"] == channel]
        if any(r["stance"] == "CONFLICTING" for r in rows):
            status = "NEUTRAL" if channel == "SENTIMENT" else "CONFLICTING"
        elif any(r["stance"] == "CONFIRMED" for r in rows):
            status = ("CONTRARIAN" if any(r.get("assessment") == "CONTRARIAN" and r["stance"] == "CONFIRMED" for r in rows)
                      else "CONFIRMING") if channel == "SENTIMENT" else "CONFIRMED"
        elif rows:
            status = "NEUTRAL"
        elif channel == "QUIVER" and any(e["channel"] == channel and e["exclusion"] == "STALE" for e in excluded):
            status = "STALE"
        else:
            status = "UNAVAILABLE"
        components[channel.lower()] = status
    confirming_groups = [g["id"] for g in groups if any(e["stance"] == "CONFIRMED" for e in g["events"])
                         and not any(e["stance"] == "CONFLICTING" for e in g["events"])]
    return components, {"groups": groups, "excluded": excluded, "independent_confirmations": len(confirming_groups),
                        "confirming_group_ids": confirming_groups,
                        "note": "Shared primary sources/events count once. Unknown provenance cannot prove independence; counts never change setup or direction."}
