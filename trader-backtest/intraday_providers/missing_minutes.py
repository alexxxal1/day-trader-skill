"""Pure retrospective minute diagnostics, never bar admission or gap repair.

Only a reviewed Alpaca SIP US-stock odd-lot rule can explain an absent bar.
Callers supply archived bytes and capture metadata; this module performs no
file or network IO and is deliberately not called by acquisition or replay.
"""
import datetime as dt
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlsplit

BAR_PRESENT = "BAR_PRESENT"
NO_BAR_CONSISTENT_WITH_TRADE_CONDITIONS = "NO_BAR_CONSISTENT_WITH_TRADE_CONDITIONS"
GAP_UNEXPLAINED = "GAP_UNEXPLAINED"
RULE_ID = "ALPACA_SIP_STOCK_ODD_LOT_MINUTE_V1"
RULE_URL = "https://docs.alpaca.markets/us/docs/market-data-faq"
_NS = 1000000000
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
_TIMESTAMP = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)")
# Narrow reviewed subset. Other conditions/tapes remain unexplained, even
# when an I tag is also present. Space is a condition, not empty text.
_TAPES = {" ": "AB", "@": "C", "F": "ABC", "I": "ABC"}
_ROWS = (
    "` `  | Regular Sale  | AB  | MD  | 🟢  | 🟢  | 🟢",
    "`@`  | Regular Sale  | CO  | MD  | 🟢  | 🟢  | 🟢",
    "`F`  | Intermarket Sweep  | ABC  | MD  | 🟢  | 🟢  | 🟢",
    "`I`  | Odd Lot Trade  | ABCO  | MD  | 🔴  | 🔴  | 🟢",
)
_POLICIES = (
    "Minute and daily bars are aggregated from trades.",
    "A trade can have more than one condition. In this case the strictest rule is applied.",
    "the bar is only emitted if none of its fields (open, high, low, close, volume) are 0.",
)


@dataclass(frozen=True)
class RawResponse:
    """Archived GET response; no credentials or authentication headers.

    content_length describes these exact stored, uncompressed bytes. If a
    capture cannot establish body completion and length, do not assert them.
    A missing/false completion flag or mismatching length prevents explanation.
    params must retain the actual wire boundaries, feed, limit and sort.
    """
    provider: str
    feed: str
    url: str
    params: dict
    body: bytes
    raw_sha256: str
    http_status: int
    retrieved_at: str
    content_length: int
    body_complete: bool


@dataclass(frozen=True)
class RuleEvidence:
    """Stored official document text, not a caller-assigned VERIFIED label."""
    source_url: str
    text: str
    text_sha256: str
    captured_at: str


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _positive(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _ns(value):
    match = _TIMESTAMP.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError("Explicit timezone and at most nine fractional digits required")
    base = dt.datetime.fromisoformat(match[1] + ("+00:00" if match[3] == "Z" else match[3]))
    delta = base.astimezone(dt.timezone.utc) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * _NS + int((match[2] or "").ljust(9, "0"))


def _utc(value):
    return (_EPOCH + dt.timedelta(seconds=value // _NS)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite JSON constant")


def _response(evidence, provider, feed, ticker, start, end, kind):
    """Return validated wire rows plus independently checked completeness."""
    proof = {"raw_sha256": None, "complete_in_api_scope": False}
    if not isinstance(evidence, RawResponse) or not isinstance(evidence.body, bytes):
        return None, proof, [kind.upper() + "_RAW_EVIDENCE_MISSING"]
    proof.update(raw_sha256=_sha(evidence.body), http_status=evidence.http_status if type(evidence.http_status) is int else None,
                 response_bytes=len(evidence.body), raw_hash_matches=evidence.raw_sha256 == _sha(evidence.body),
                 body_completion_asserted=evidence.body_complete is True,
                 content_length=evidence.content_length if type(evidence.content_length) is int else None)
    prefix = kind.upper() + "_"
    try:
        if provider != "ALPACA" or evidence.provider.upper() != provider or evidence.feed.upper() != feed:
            raise ValueError("SOURCE_IDENTITY_MISMATCH")
        expected_url = "https://data.alpaca.markets/v2/stocks/" + ticker + "/" + kind
        url = urlsplit(evidence.url)
        if evidence.url != expected_url or url.username or url.password or url.query or url.fragment:
            raise ValueError("REQUEST_ENDPOINT_MISMATCH")
        if evidence.http_status != 200:
            raise ValueError("HTTP_NOT_200")
        if evidence.raw_sha256 != proof["raw_sha256"]:
            raise ValueError("RAW_HASH_MISMATCH")
        captured_after_end = _ns(evidence.retrieved_at) >= end
        params = evidence.params
        allowed = {"start", "end", "feed", "limit", "sort", "asof", "page_token"}
        if kind == "bars":
            allowed |= {"timeframe", "adjustment"}
        if not isinstance(params, dict) or set(params) - allowed:
            raise ValueError("UNSUPPORTED_REQUEST_PARAMETERS")
        if params.get("feed") != feed.lower() or params.get("sort") not in ("asc", "desc"):
            raise ValueError("REQUEST_FEED_OR_SORT_MISMATCH")
        if type(params.get("limit")) is not int or not 1 <= params["limit"] <= 10000:
            raise ValueError("REQUEST_LIMIT_UNKNOWN")
        begin, finish = _ns(params.get("start")), _ns(params.get("end"))
        if kind == "trades" and (begin != start or finish != end - 1):
            raise ValueError("EXACT_TRADE_INTERVAL_REQUIRED")
        if kind == "bars" and (begin > start or finish < end - 1 or
                               params.get("timeframe") not in ("1Min", "1m") or params.get("adjustment") != "raw"):
            raise ValueError("BAR_INTERVAL_OR_CONVENTION_MISMATCH")
        if params.get("asof") is not None:
            dt.date.fromisoformat(params["asof"])
        body = json.loads(evidence.body.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_invalid_constant)
        if not isinstance(body, dict) or body.get("symbol") != ticker or kind not in body:
            raise ValueError("RESPONSE_IDENTITY_OR_SCHEMA_MISMATCH")
        rows = body[kind] if body[kind] is not None else []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("INVALID_RECORDS")
        times = [_ns(row.get("t")) for row in rows]
        if any(not begin <= time <= finish for time in times):
            raise ValueError("RECORD_OUTSIDE_REQUEST")
        if times != sorted(times, reverse=params["sort"] == "desc"):
            raise ValueError("UNSORTED_RECORDS")
        if kind == "bars" and any(time % (60 * _NS) for time in times):
            raise ValueError("BAR_OFF_MINUTE_GRID")
    except (ValueError, TypeError, AttributeError, OverflowError, UnicodeError) as exc:
        # Do not echo arbitrary input, URL userinfo, params or response text.
        known = str(exc) if re.fullmatch(r"[A-Z][A-Z0-9_]*", str(exc)) else "INVALID_METADATA_OR_JSON"
        return None, proof, [prefix + known]
    errors = []
    if not captured_after_end:
        errors.append(prefix + "INTERVAL_NOT_ENDED_AT_CAPTURE")
    if evidence.body_complete is not True or type(evidence.content_length) is not int or evidence.content_length != len(evidence.body):
        errors.append(prefix + "BODY_COMPLETENESS_NOT_ESTABLISHED")
    if "next_page_token" not in body or body["next_page_token"] not in (None, "") or params.get("page_token"):
        errors.append(prefix + "PAGINATION_INCOMPLETE_OR_UNKNOWN")
    if len(rows) > params["limit"]:
        errors.append(prefix + "LIMIT_EXCEEDED")
    if kind == "bars" and len(set(times)) != len(times):
        errors.append(prefix + "DUPLICATE_BARS")
    proof.update(provider=provider, feed=feed, endpoint=expected_url,
                 request={key: params[key] for key in sorted(params) if key != "page_token"},
                 retrieved_at=evidence.retrieved_at, complete_in_api_scope=not errors,
                 capture_after_interval_end=captured_after_end,
                 records=len(rows), next_page_token_present=bool(body.get("next_page_token")),
                 pagination_field_present="next_page_token" in body)
    return rows, proof, errors


def _rule(source):
    if not isinstance(source, RuleEvidence):
        return None, ["RULE_SOURCE_MISSING"]
    try:
        text_hash = _sha(source.text.encode("utf-8"))
        _ns(source.captured_at)
        # Preserve the SPACE code. Only remove the archived extractor's line labels.
        text = re.sub(r"(?m)^L\d+: ?", "", source.text)
        if source.source_url != RULE_URL or source.text_sha256 != text_hash:
            raise ValueError()
        if not all(row in text for row in _ROWS) or not all(policy in text for policy in _POLICIES):
            raise ValueError()
    except (ValueError, TypeError, AttributeError, UnicodeError, OverflowError):
        return None, ["RULE_SOURCE_UNSUPPORTED_OR_UNBOUND"]
    return {"id": RULE_ID, "source_url": RULE_URL, "text_sha256": text_hash,
            "captured_at": source.captured_at, "scope": "Alpaca SIP US stocks, tapes A/B/C, minute bars",
            "rule": "I excludes all OHLC price updates; strictest condition wins; volume alone does not emit a bar"}, []


def classify_minute(provider, feed, ticker, start, end, bars_response=None,
                    trades_response=None, rule_evidence=None):
    """Classify one [start,end) minute without changing any reconstruction.

    Missing-bar explanation requires complete stored bars and trades responses.
    Trades must query this exact minute with Alpaca's inclusive wire end one
    nanosecond before end. A broader complete bars query may contain the minute.
    BAR_PRESENT records observation only, not OHLCV validity or admissibility.
    Captures and rule provenance are caller supplied and are not authenticated
    by a hash; completeness is limited to the documented API response scope.
    """
    if not all(isinstance(v, str) for v in (provider, feed, ticker)):
        raise ValueError("Provider, feed and ticker must be explicit")
    provider, feed, ticker = provider.upper(), feed.upper(), ticker.upper()
    if not re.fullmatch(r"[A-Z0-9_-]{1,32}", provider) or not re.fullmatch(r"[A-Z0-9_-]{1,32}", feed) or not re.fullmatch(r"[A-Z0-9.-]{1,20}", ticker):
        raise ValueError("Invalid source identity")
    begin, finish = _ns(start), _ns(end)
    if finish - begin != 60 * _NS or begin % (60 * _NS):
        raise ValueError("One aligned half-open minute required")
    result = dict(state=GAP_UNEXPLAINED, provider=provider, feed=feed, ticker=ticker,
        interval={"start": _utc(begin), "end_exclusive": _utc(finish), "timezone": "UTC"},
        reasons=[], evidence={}, reconstruction_blocking=True, may_bridge_gap=False,
        bars_created=0, native_admission_assessed=False, historical_pit_availability="NOT_ESTABLISHED",
        limitations=["API-scope completeness is not absolute market/tape completeness",
                     "Hashes bind supplied bytes, not their authenticity or completeness alone",
                     "Retrieval and document-capture times do not establish historical availability",
                     "Explanation never fills a slot or changes prefix, frame, provider or trigger rules"])
    bars, proof, errors = _response(bars_response, provider, feed, ticker, begin, finish, "bars")
    result["evidence"]["bars"] = proof
    if bars is not None and any(_ns(row["t"]) == begin for row in bars):
        result.update(state=BAR_PRESENT, reasons=["RAW_BAR_OBSERVED"], reconstruction_blocking=False)
        result["evidence"]["bars"]["capture_warnings"] = errors
        return result
    result["reasons"].extend(errors)
    if feed != "SIP" or provider != "ALPACA":
        result["reasons"].append("NO_REVIEWED_CONDITION_RULE_FOR_SOURCE")
    trades, proof, errors = _response(trades_response, provider, feed, ticker, begin, finish, "trades")
    result["evidence"]["trades"] = proof
    result["reasons"].extend(errors)
    rule, errors = _rule(rule_evidence)
    result["evidence"]["rule"] = rule
    result["reasons"].extend(errors)
    if result["reasons"]:
        return result
    if bars_response.params.get("asof") != trades_response.params.get("asof"):
        result["reasons"].append("ASOF_IDENTITY_CONVENTION_MISMATCH")
        return result
    if not trades:
        result["reasons"].append("EMPTY_TRADES_DO_NOT_PROVE_NO_TRADING")
        return result
    conditions, exchanges, tapes = Counter(), Counter(), Counter()
    seen, reasons, price_updates = set(), set(), 0
    for trade in trades:
        codes, tape, exchange = trade.get("c"), trade.get("z"), trade.get("x")
        numeric = all(_positive(trade.get(k)) for k in ("p", "s"))
        if (not numeric or type(trade.get("i")) is not int or trade["i"] < 0 or
                not isinstance(exchange, str) or not re.fullmatch(r"[A-Z0-9]", exchange) or
                not isinstance(codes, list) or not codes or any(not isinstance(c, str) or len(c) != 1 for c in codes) or
                len(codes) != len(set(codes))):
            reasons.add("INVALID_TRADE_SCHEMA")
            continue
        if (exchange, trade["i"]) in seen:
            reasons.add("DUPLICATE_TRADE")
        seen.add((exchange, trade["i"]))
        conditions.update(codes); exchanges[exchange] += 1
        if not isinstance(tape, str) or tape not in ("A", "B", "C"):
            reasons.add("UNSUPPORTED_TAPE")
            continue
        tapes[tape] += 1
        if any(code not in _TAPES for code in codes):
            reasons.add("UNKNOWN_OR_UNSUPPORTED_CONDITION")
        elif any(tape not in _TAPES[code] for code in codes):
            reasons.add("CONDITION_TAPE_MISMATCH")
        elif "I" not in codes:
            price_updates += 1
    result["evidence"]["trades"].update(trade_count=len(trades), condition_counts=dict(conditions),
        exchange_counts=dict(exchanges), tape_counts=dict(tapes), price_updating_trades=price_updates)
    if price_updates:
        reasons.add("PRICE_UPDATING_TRADE_OBSERVED")
    if reasons:
        result["reasons"] = sorted(reasons)
    else:
        result.update(state=NO_BAR_CONSISTENT_WITH_TRADE_CONDITIONS,
                      reasons=["ALL_RETURNED_TRADES_EXCLUDE_OHLC_PRICE_UPDATES_UNDER_REVIEWED_RULE"])
    return result
