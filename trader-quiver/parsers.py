"""Parse public Quiver tables and local evidence without executing page code."""

import ast
import csv
import datetime as dt
import io
import json
import math
import re
from html.parser import HTMLParser

MAX_ROWS = 2000
MAX_TEXT = 8 * 1024 * 1024
UTC = dt.timezone.utc
EST = dt.timezone(dt.timedelta(hours=-5), "EST")
MISSING = ("", "-", "n/a", "nat", "none", "null", "unknown", "unbekannt")
EVENT_FIELDS = ("transactiondate", "traded", "tradeddate", "eventdate", "date")
DISCLOSURE_FIELDS = ("disclosuredate", "filedate", "filingdate", "fileddate", "filed",
                     "reportdate", "publicationdate", "publisheddate", "disclosedest", "disclosed")


class ParseError(Exception):
    pass


def key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def missing(value):
    return value is None or (isinstance(value, str) and value.strip().lower() in MISSING)


def timestamp(value):
    """Analysis clocks require an explicit UTC offset."""
    if not isinstance(value, str):
        raise ParseError("Timestamp requires ISO-8601 with a UTC offset")
    try:
        point = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ParseError("Timestamp requires ISO-8601 with a UTC offset") from None
    if point.tzinfo is None:
        raise ParseError("Timestamp requires ISO-8601 with a UTC offset")
    return point


def date_value(value, est=False, date_only=False):
    """Return a calendar date and, only when justified, an aware timestamp."""
    if missing(value):
        return None, None, "UNKNOWN"
    if not isinstance(value, str):
        raise ParseError("Invalid date value")
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return dt.date.fromisoformat(value), None, "DATE"
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return dt.datetime.strptime(value, fmt).date(), None, "DATE"
        except ValueError:
            pass
    try:
        point = dt.datetime.strptime(value, "%b %d, %Y (%I:%M %p)")
    except ValueError:
        try:
            point = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ParseError("Unsupported date format; use ISO-8601") from None
    if date_only:
        return point.date(), None, "DATE"
    if point.tzinfo is None and est:
        point = point.replace(tzinfo=EST)
    return (point.date(), point if point.tzinfo else None,
            "TIMESTAMP" if point.tzinfo else "DATETIME_WITHOUT_TIMEZONE")


def _literal(node):
    # A small data-only subset also accepts JavaScript null/true/false.
    if isinstance(node, ast.Constant) and (node.value is None or type(node.value) in (str, int, float, bool)):
        return node.value
    if isinstance(node, ast.Name) and node.id in ("null", "true", "false"):
        return {"null": None, "true": True, "false": False}[node.id]
    if isinstance(node, ast.List):
        return [_literal(item) for item in node.elts]
    if isinstance(node, ast.Dict):
        return {_literal(k): _literal(v) for k, v in zip(node.keys, node.values)}
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        number = _literal(node.operand)
        if type(number) in (int, float):
            return -number if isinstance(node.op, ast.USub) else number
    raise ParseError("Unsupported executable or nonliteral page data")


def array_literal(script, variable):
    start = re.search(r"\b(?:let|const|var)\s+" + re.escape(variable) + r"\s*=\s*(\[)", script)
    if not start:
        return None
    begin = start.start(1)
    depth, quote, escaped = 0, None, False
    for index in range(begin, len(script)):
        char = script[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                try:
                    tree = ast.parse(script[begin:index + 1], mode="eval")
                    result = _literal(tree.body)
                except (SyntaxError, ValueError, TypeError, RecursionError):
                    raise ParseError("Public table data changed or is not a literal array") from None
                if len(result) > MAX_ROWS:
                    raise ParseError("Public table exceeds the row limit")
                return result
    raise ParseError("Unterminated public table array")


def _restricted(attrs):
    values = dict(attrs)
    classes = values.get("class", "")
    style = values.get("style", "").replace(" ", "").lower()
    return ("hidden" in values or values.get("aria-hidden") == "true"
            or bool(re.search(r"paywall|blur|locked|gated|premium-only|login-required", classes, re.I))
            or "filter:blur" in style or "display:none" in style
            or any(values.get(k) in ("true", "1") for k in ("data-paywall", "data-locked")))


class PublicPage(HTMLParser):
    """Only the named public table and its data scripts are relevant."""

    def __init__(self, table_id):
        super().__init__(convert_charrefs=True)
        self.table_id, self.table_depth = table_id, 0
        self.stack, self.scripts, self.headers, self.rows = [], [], [], []
        self.found, self.restricted = False, False
        self.script, self.cell, self.row = None, None, None

    def handle_starttag(self, tag, attrs):
        if tag not in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"):
            self.stack.append((tag, _restricted(attrs)))
        if tag == "script":
            self.script = []
        if tag == "table" and dict(attrs).get("id") == self.table_id:
            self.found = True
            self.table_depth = len(self.stack)
            self.restricted = any(locked for _, locked in self.stack)
        if self.table_depth:
            if _restricted(attrs):
                self.restricted = True
            if tag == "tr":
                self.row = []
            elif tag in ("th", "td"):
                self.cell = (tag, [])

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        if self.cell is not None:
            self.cell[1].append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.scripts.append("".join(self.script))
            self.script = None
        if self.table_depth:
            if tag in ("th", "td") and self.cell is not None:
                text = " ".join(" ".join(self.cell[1]).split())
                if self.cell[0] == "th":
                    self.headers.append(text)
                elif self.row is not None:
                    self.row.append(text)
                self.cell = None
            elif tag == "tr" and self.row is not None:
                if self.row:
                    self.rows.append(self.row)
                self.row = None
            elif tag == "table":
                self.table_depth = 0
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def parse_public_html(text, dataset):
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        raise ParseError("Public page exceeds the text limit")
    if dataset not in ("CONGRESS", "INSIDER"):
        raise ParseError("Unsupported dataset")
    congress = dataset == "CONGRESS"
    page = PublicPage("recentTradesTable" if congress else "recentInsiderTransactionsTable")
    page.feed(text)
    expected = {"stock", "filed", "traded"} if congress else {"stock", "date", "disclosedest"}
    if not page.found or not expected.issubset({key(h) for h in page.headers}):
        raise ParseError("Public table or date-column schema unavailable; no bypass attempted")
    if page.restricted:
        raise ParseError("Public table is hidden or access-restricted; no bypass attempted")
    if page.rows:
        if len(page.rows) > MAX_ROWS:
            raise ParseError("Public table exceeds the row limit")
        result = []
        for cells in page.rows:
            if len(cells) != len(page.headers):
                raise ParseError("Public table column count changed")
            row = dict(zip(page.headers, cells))
            row["Ticker"] = row["Stock"].split()[0] if row.get("Stock") else None
            row["_public_est"] = not congress
            row["_public_congress_date_only"] = congress
            result.append(row)
        return result
    variable = "recentTradesData" if congress else "recentInsiderTransactionsData"
    arrays = [array_literal(script, variable) for script in page.scripts
              if re.search(r"\b(?:let|const|var)\s+" + variable + r"\s*=", script)]
    if len(arrays) != 1 or not isinstance(arrays[0], list):
        raise ParseError("Public table data missing or ambiguous")
    result = []
    for item in arrays[0]:
        if congress:
            if not isinstance(item, list) or len(item) != 16:
                raise ParseError("Congress table column layout changed")
            row = {"Ticker": item[0], "Asset": item[1], "Transaction": item[3],
                   "Amount": item[4], "Politician": item[5], "Chamber": item[6],
                   "Filed": item[8], "Traded": item[9], "Description": item[10],
                   "id": item[11], "_public_congress_date_only": True}
        else:
            if not isinstance(item, dict) or not {"transactionDate", "fileDate", "issuerTradingSymbol"}.issubset(item):
                raise ParseError("Insider table field layout changed")
            row = dict(item, _public_est=True)
        result.append(row)
    return result


def json_rows(text):
    def reject_constant(value):
        raise ParseError("Nonfinite JSON numbers are not supported")
    try:
        document = json.loads(text, parse_constant=reject_constant)
    except (ValueError, TypeError, RecursionError):
        raise ParseError("Invalid JSON document") from None
    if isinstance(document, dict):
        containers = [document[k] for k in ("records", "data", "rows") if k in document]
        if len(containers) != 1:
            raise ParseError("JSON needs one records, data or rows array")
        document = containers[0]
    if not isinstance(document, list) or len(document) > MAX_ROWS or any(not isinstance(r, dict) for r in document):
        raise ParseError("Expected a bounded array of record objects")
    return document


def parse_input(text, format="auto"):
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        raise ParseError("Input exceeds the text limit")
    text = text.lstrip("\ufeff").strip()
    if format == "json" or (format == "auto" and text.startswith(("[", "{"))):
        return json_rows(text)
    if not text:
        raise ParseError("Input is empty")
    if format not in ("auto", "csv", "text"):
        raise ParseError("Unsupported input format")
    lines = text.splitlines()
    if "|" in lines[0]:
        lines = [line.strip().strip("|").strip() for line in lines if line.strip()]
        lines = [line for line in lines if not re.fullmatch(r"[\s|:\-]+", line)]
        text = "\n".join(lines)
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        reader = csv.DictReader(io.StringIO(text), dialect=dialect, skipinitialspace=True)
        headers = reader.fieldnames or []
        if not ({key(h) for h in headers} & set(EVENT_FIELDS + DISCLOSURE_FIELDS)):
            raise ParseError("A table header with explicit date columns is required")
        if len({key(h) for h in headers}) != len(headers):
            raise ParseError("Duplicate input columns")
        rows = []
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise ParseError("Input row does not match its header")
            rows.append({k.strip(): v.strip() for k, v in row.items()})
            if len(rows) > MAX_ROWS:
                raise ParseError("Input exceeds the row limit")
        return rows
    except csv.Error:
        raise ParseError("Expected CSV, TSV or a copied table; transcribe prose/images into JSON") from None


def _pick(row, fields):
    for field in fields:
        if field in row and not missing(row[field]):
            return row[field]
    return None


def normalize_record(raw, dataset, source, reference, now, max_age_days=7):
    if dataset not in ("CONGRESS", "INSIDER") or source not in ("QUIVER_API", "PUBLIC_WEB", "USER_INPUT"):
        raise ParseError("Unsupported dataset or source")
    if (not isinstance(raw, dict) or any(not isinstance(k, str) for k in raw)
            or not isinstance(now, dt.datetime) or now.tzinfo is None):
        raise ParseError("Record objects and an aware analysis clock are required")
    if not math.isfinite(max_age_days) or max_age_days < 0:
        raise ParseError("Freshness threshold must be finite and nonnegative")
    row = {key(k): v for k, v in raw.items()}
    issues = [] if len(row) == len(raw) else ["CONFLICTING_FIELD_NAMES"]
    dates, points, precision = {}, {}, {}
    for field, aliases in (("transaction", EVENT_FIELDS), ("disclosure", DISCLOSURE_FIELDS)):
        value = _pick(row, aliases)
        try:
            day, point, unit = date_value(value, est=((source == "PUBLIC_WEB" and raw.get("_public_est") is True)
                                                     or "disclosedest" in row),
                                          date_only=(source == "PUBLIC_WEB" and raw.get("_public_congress_date_only") is True))
        except (ParseError, ValueError):
            day, point, unit = None, None, "UNKNOWN"
            issues.append("INVALID_%s_DATE" % field.upper())
        dates[field], points[field], precision[field] = day, point, unit
        if day is None and missing(value):
            issues.append("MISSING_%s_DATE" % field.upper())
        # Conflicting explicit aliases are not silently resolved by precedence.
        for alias in aliases:
            if alias not in row or missing(row[alias]) or row[alias] == value:
                continue
            try:
                other, _, _ = date_value(row[alias])
                if other != day:
                    issues.append("CONFLICTING_%s_DATES" % field.upper())
            except (ParseError, ValueError):
                issues.append("INVALID_%s_DATE" % field.upper())
    ticker = _pick(row, ("ticker", "issuertradingsymbol", "symbol", "stock"))
    if isinstance(ticker, str):
        ticker = ticker.strip().upper()
    if not isinstance(ticker, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-^/]{0,14}", ticker):
        ticker = None
        issues.append("MISSING_OR_INVALID_TICKER")
    event, disclosed = dates["transaction"], dates["disclosure"]
    disclosure_point = points["disclosure"]
    today = now.astimezone(UTC).date()
    event_point = points["transaction"]
    event_today = now.astimezone(event_point.tzinfo).date() if event_point else today
    event_age = (event_today - event).days if event else None
    local_today = now.astimezone(disclosure_point.tzinfo).date() if disclosure_point else today
    disclosure_age = (local_today - disclosed).days if disclosed else None
    age_seconds = (now - disclosure_point).total_seconds() if disclosure_point else None
    if (event_age is not None and event_age < 0) or (event_point is not None and event_point > now):
        issues.append("FUTURE_TRANSACTION_DATE")
        event_age = None
    if (disclosure_age is not None and disclosure_age < 0) or (age_seconds is not None and age_seconds < 0):
        issues.append("FUTURE_DISCLOSURE_DATE")
        disclosure_age, age_seconds = None, None
    raw_delay = (disclosed - event).days if event and disclosed else None
    if points["transaction"] is not None and disclosure_point is not None:
        raw_delay = (disclosure_point.astimezone(UTC).date() - points["transaction"].astimezone(UTC).date()).days
    delay = raw_delay
    delay_status = "VALID" if delay is not None else "UNKNOWN"
    reversed_instants = (points["transaction"] is not None and disclosure_point is not None
                         and disclosure_point < points["transaction"])
    if (raw_delay is not None and raw_delay < 0) or reversed_instants:
        issues.append("DISCLOSURE_BEFORE_TRANSACTION")
        delay, delay_status = None, "INCONSISTENT"
    inconsistent = any(not item.startswith("MISSING") for item in issues)
    quality = "INCONSISTENT" if inconsistent else "INCOMPLETE" if issues else "OK"
    if quality == "INCONSISTENT":
        delay, delay_status = None, "INCONSISTENT"
        freshness = "INCONSISTENT"
    elif disclosure_age is None:
        freshness = "UNKNOWN"
    else:
        age = age_seconds / 86400 if age_seconds is not None else disclosure_age
        freshness = "FRESH" if age <= max_age_days else "STALE"
    action = _pick(row, ("transaction", "transactioncode", "purchasesale", "type"))
    action = {"P": "Purchase", "S": "Sale"}.get(action, action) if isinstance(action, str) else None
    return {
        "ticker": ticker, "dataset": dataset, "source": source, "source_reference": reference,
        "person": _pick(row, ("politician", "representative", "rptownername", "name", "member")),
        "transaction": action, "amount": _pick(row, ("amount", "range", "transactionvalue", "valueest")),
        "transaction_date": event.isoformat() if event else None,
        "transaction_timestamp": points["transaction"].isoformat() if points["transaction"] else None,
        "disclosure_date": disclosed.isoformat() if disclosed else None,
        "disclosure_timestamp": disclosure_point.isoformat() if disclosure_point else None,
        "transaction_date_precision": precision["transaction"], "disclosure_date_precision": precision["disclosure"],
        "transaction_age_days": event_age, "disclosure_age_days": disclosure_age,
        "disclosure_age_seconds": age_seconds, "freshness": freshness,
        "disclosure_delay_days": delay, "raw_disclosure_delay_days": raw_delay,
        "disclosure_delay_status": delay_status, "quality": quality, "issues": sorted(set(issues)),
        "checked_at": now.isoformat(), "raw": {k: v for k, v in raw.items() if not k.startswith("_public_")},
    }
