#!/usr/bin/env python3
"""Read Quiver disclosures via public webpages, optional official API, or local input.

Research only. No broker, orders, automatic polling, or implicit source fallback.
Python 3.8+, standard library only.
"""

import argparse
import datetime as dt
import importlib.util
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def local_module(name):
    spec = importlib.util.spec_from_file_location("quiver_" + name, os.path.join(HERE, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


parsers = local_module("parsers")
sources = local_module("sources")


def analyze(rows, dataset, source, reference, now, max_age_days=7, limit=20, ticker=None, input_kind=None):
    if (not isinstance(rows, list) or len(rows) > parsers.MAX_ROWS
            or type(limit) is not int or not 1 <= limit <= parsers.MAX_ROWS):
        raise parsers.ParseError("Expected a bounded record array")
    records = [parsers.normalize_record(row, dataset, source, reference, now, max_age_days) for row in rows]
    if ticker:
        records = [row for row in records if row["ticker"] == ticker]
    summary = {"fetched": len(rows), "matching": len(records), "returned": min(limit, len(records)),
               "inconsistent": sum(r["quality"] == "INCONSISTENT" for r in records),
               "incomplete": sum(r["quality"] == "INCOMPLETE" for r in records),
               "stale": sum(r["freshness"] == "STALE" for r in records)}
    return {"created_by": "trader-quiver", "mode": "research", "status": "OK",
            "dataset": dataset, "source": source, "source_reference": reference,
            "input_kind": input_kind, "checked_at": now.isoformat(), "max_age_days": max_age_days,
            "summary": summary, "records": records[:limit]}


def render(result):
    def cell(value):
        return re.sub(r"[\r\n\t|]+", " ", str(value if value is not None else "UNKNOWN"))
    if result["status"] != "OK":
        return "DATA UNAVAILABLE | %s | %s" % (result["source"], result["reason"])
    lines = ["Ticker | Datentyp | Ereignisdatum | Veröffentlichungsdatum | Quelle | Alter (Tage) | Disclosure-Verzögerung (Tage) | Qualität"]
    for row in result["records"]:
        delay = row["disclosure_delay_days"]
        if row["disclosure_delay_status"] == "INCONSISTENT":
            delay = "INCONSISTENT (raw=%s)" % row["raw_disclosure_delay_days"]
        lines.append(" | ".join(cell(v) for v in (
            row["ticker"], row["dataset"], row["transaction_date"], row["disclosure_date"], row["source"],
            row["disclosure_age_days"], delay, row["quality"])))
        if row["issues"]:
            lines.append("  " + ", ".join(row["issues"]))
    lines.append("Quelle: %s; geprüft: %s; %s" % (cell(result["source_reference"]), result["checked_at"], result["summary"]))
    lines.append("Date-only ages/delays are calendar days; exact publication age is available in JSON only with a known timezone.")
    return "\n".join(lines)


def read_input(path):
    if os.path.splitext(path)[1].lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf"):
        raise parsers.ParseError("Transcribe the supplied screenshot/PDF into JSON or a table first; no OCR is bundled")
    try:
        with open(path, "rb") as stream:
            raw = stream.read(sources.MAX_BYTES + 1)
        if len(raw) > sources.MAX_BYTES:
            raise parsers.ParseError("Input exceeds the 8 MiB limit")
        return raw.decode("utf-8-sig")
    except (OSError, UnicodeError):
        raise parsers.ParseError("Cannot read UTF-8 input file") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("fetch", "input"):
        sub = commands.add_parser(name)
        sub.add_argument("--dataset", choices=("congress", "insider"), required=True)
        sub.add_argument("--json", action="store_true")
        sub.add_argument("--now", help="ISO-8601 with explicit UTC offset, for reproducible analysis")
        sub.add_argument("--max-age-days", type=float, default=7)
        sub.add_argument("--limit", type=int, default=20, help="Returned records, 1-2000; does not trigger more requests")
        sub.add_argument("--ticker", help="Exact ticker filter, applied locally")
        if name == "fetch":
            sub.add_argument("--source", choices=("public-web", "api"), default="public-web")
        else:
            sub.add_argument("--file", required=True)
            sub.add_argument("--format", choices=("auto", "json", "csv", "text"), default="auto")
            sub.add_argument("--kind", choices=("json", "csv", "text", "screenshot"), default=None,
                             help="Evidence kind; screenshot means a user-supplied transcription")
    args = parser.parse_args(argv)
    dataset = args.dataset.upper()
    source = "USER_INPUT" if args.command == "input" else "QUIVER_API" if args.source == "api" else "PUBLIC_WEB"
    reference = args.file if args.command == "input" else (sources.API_URLS if source == "QUIVER_API" else sources.PUBLIC_URLS)[dataset]
    received_response = False
    try:
        now = parsers.timestamp(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
        if (not math.isfinite(args.max_age_days) or args.max_age_days < 0 or not 1 <= args.limit <= parsers.MAX_ROWS
                or (args.ticker and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-^/]{0,14}", args.ticker))):
            raise parsers.ParseError("Invalid limit, freshness threshold or ticker")
        if args.command == "input":
            text = read_input(args.file)
            rows = parsers.parse_input(text, args.format)
            # The caller, never the contents of an imported row, sets provenance.
            kind = args.kind or ("json" if text.lstrip().startswith(("{", "[")) else "text")
        else:
            payload = sources.fetch_api(dataset) if source == "QUIVER_API" else sources.fetch_public(dataset)
            received_response = True
            rows = parsers.json_rows(payload["body"]) if source == "QUIVER_API" else parsers.parse_public_html(payload["body"], dataset)
            kind = None
        result = analyze(rows, dataset, source, reference, now, args.max_age_days,
                         args.limit, args.ticker.upper() if args.ticker else None, kind)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) if args.json else render(result))
        return 0
    except sources.SourceUnavailable as exc:
        result = {"created_by": "trader-quiver", "status": "DATA UNAVAILABLE", "source": source,
                  "source_reference": reference, "dataset": dataset, "reason": str(exc), "records": []}
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
        return 1
    except (parsers.ParseError, TypeError, ValueError, RecursionError):
        # Never echo untrusted file contents or API responses that could hold secrets.
        if args.command == "fetch" and received_response:
            result = {"created_by": "trader-quiver", "status": "DATA UNAVAILABLE", "source": source,
                      "source_reference": reference, "dataset": dataset,
                      "reason": "Quiver response schema or date data could not be parsed", "records": []}
            print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
            return 1
        print("trader-quiver: Invalid input, date clock or options; use UTF-8 JSON/CSV/table input, transcribe images first, and give --now an explicit offset", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
