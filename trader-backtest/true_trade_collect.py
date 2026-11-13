#!/usr/bin/env python3
"""Acquire a frozen recent pilot; only this command and intraday_audit use HTTP."""
import argparse
import datetime as dt
import json
from pathlib import Path
import time

from common import Clock, EPOCH, calendar_2024_2027, digest, iso, read, write
from intraday_audit import describe, request_chart


def freeze(path, value):
    path = Path(path)
    if path.exists() and read(path) != value:
        raise ValueError("frozen input differs; choose a new run directory: " + str(path))
    write(path, value)


def collect(config, output, audit_path, end):
    output = Path(output)
    audit = read(audit_path)
    if not audit.get("yahoo", {}).get("probes"):
        raise ValueError("run the data coverage audit before acquisition")
    freeze(output / "frozen-config.json", config)
    freeze(output / "frozen-acquisition.json", {"end": end, "audit_sha256": digest(audit),
        "config_sha256": digest(config), "requests": {"1d": "2y", "5m": "60d", "1m": "7d"},
        "universe": config["universe"], "extended_hours": True})
    clock = Clock(calendar_2024_2027())
    if end not in clock.sessions:
        raise ValueError("end must be a supplied exchange session")
    doc = {"schema_version": 1, "sessions": clock.sessions, "universe": config["universe"],
        "bars": {}, "splits": {}, "evidence": {"events": [], "metadata": []},
        "provenance": {"retrospective_snapshot": True, "point_in_time_universe_includes_delisted": False,
            "price_basis": "YAHOO_SPLIT_ADJUSTED", "audit_sha256": digest(audit),
            "historical_certification": "NOT_HISTORICALLY_PIT_CERTIFIED",
            "universe_scope": config["universe_source"], "end": end,
            "catalysts": "No verified historical publication/observation archive; none inferred",
            "records": []}}
    halted = False
    for symbol in config["universe"]:
        for interval, span in (("1d", "2y"), ("5m", "60d"), ("1m", "7d")):
            if halted:
                doc["provenance"]["records"].append({"symbol": symbol, "interval": interval, "status": "NOT_REQUESTED_AFTER_SOURCE_FAILURE"})
                continue
            record, body = request_chart(symbol, {"range": span, "interval": interval,
                "includePrePost": "true", "events": "splits,div"}, output / "raw" / (symbol + "-" + interval + ".json.gz"))
            summary = dict(record, symbol=symbol, interval=interval, **describe(body, interval))
            doc["provenance"]["records"].append(summary)
            if record["http_status"] in (429, 401, 403) or record["http_status"] is None:
                halted = True
            if summary["status"] != "AVAILABLE":
                continue
            result = json.loads(body)["chart"]["result"][0]
            meta = result["meta"]
            if meta.get("symbol", "").upper() != symbol.replace(".", "-") or meta.get("dataGranularity") != interval:
                raise ValueError("provider identity/granularity mismatch")
            # Current vendor identity is an audit field, never PIT admission.
            summary["current_identity"] = {k: meta.get(k) for k in ("currency", "instrumentType", "exchangeTimezoneName")}
            doc["splits"].setdefault(symbol, {}).update((result.get("events") or {}).get("splits", {}))
            quotes = result["indicators"]["quote"][0]
            rows = []
            for i, t in enumerate(result["timestamp"]):
                start = EPOCH + dt.timedelta(seconds=t)
                local = clock.to_eastern(start)
                day = local.date().isoformat()
                if day > end or day not in clock.sessions:
                    continue
                finish = clock.close(day) if interval == "1d" else start + dt.timedelta(minutes=int(interval[:-1]))
                if interval == "1d":
                    start = clock.eastern_to_utc(dt.datetime.combine(local.date(), dt.time(9, 30)))
                values = {k: (quotes.get(col) or [])[i] if i < len(quotes.get(col) or []) else None
                          for k, col in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume"))}
                rows.append(dict(values, start=iso(start), end=iso(finish), available_at=iso(finish),
                    availability_assumption="retrospective bar-close proxy; NOT_HISTORICALLY_PIT_CERTIFIED"))
            doc["bars"].setdefault(symbol, {})[interval] = rows
            print(symbol, interval, len(rows), flush=True)
            time.sleep(0.5)
    write(output / "dataset.json", doc)
    return doc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--audit", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--end", required=True, help="Last completed exchange session, explicit and frozen")
    args = parser.parse_args()
    collect(read(args.config), args.output, args.audit, args.end)
