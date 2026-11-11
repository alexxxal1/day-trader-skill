#!/usr/bin/env python3
"""Full scanner universe, daily-only acquisition; per-ticker immutable archives."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

from common import digest, read, write


def validate_response(body, symbol):
    doc = json.loads(body)
    result = doc["chart"]["result"][0]
    if result["meta"].get("dataGranularity") != "1d":
        raise ValueError("WRONG_GRANULARITY:" + str(result["meta"].get("dataGranularity")))
    if result["meta"].get("symbol", "").upper() != symbol.replace(".", "-"):
        raise ValueError("SYMBOL_MISMATCH")
    if not result.get("timestamp"):
        raise ValueError("EMPTY_HISTORY")
    q = result["indicators"]["quote"][0]
    if any(k not in q for k in ("open", "high", "low", "close", "volume")):
        raise ValueError("MISSING_OHLCV_COLUMNS")
    return result


def collect(classification, output, end="2026-11-06", workers=8):
    output = Path(output)
    raw = output / "raw-daily"
    raw.mkdir(parents=True, exist_ok=True)
    universe = sorted(dict(classification["kept"]))
    frozen = {"layer": "DAILY_HISTORY", "end": end, "period1": 0,
        "universe": universe, "directory_created": classification["created"],
        "directory_classification_sha256": digest(classification),
        "universe_rule": "Existing scanner classify/build_universe, unchanged",
        "common_daily_history": 260, "randomizations": 1000, "seed": 20261109,
        "horizons": [1, 3, 5, 10, 20, 60], "daily_anchor": "completed daily close",
        "intraday_required": False, "start_policy": "Earliest SPY daily close with >=2 common eligible stocks; latest completed session",
        "cost_policy": "Gross filter forward price returns, zero execution costs for every strategy and random control; no invented trades",
        "calendar": "XNYS exchange_calendars 4.11.2; saved UTC schedule; historical DST respected",
        "strategies": ["MINERVINI", "BREAKOUT_CANDIDATE", "BREAKOUT_SETUP_READY", "CONFLUENCE_CANDIDATE", "CONFLUENCE_SETUP_READY"],
        "parameters": "Existing Minervini and Qullamaggie defaults, no tuning"}
    path = output / "frozen-acquisition.json"
    if path.exists() and read(path) != frozen:
        raise ValueError("frozen acquisition differs; choose a new output folder")
    write(path, frozen)
    finish = int(dt.datetime.fromisoformat(end).replace(tzinfo=dt.timezone.utc).timestamp()) + 86400

    def fetch(sym):
        filename = raw / (sym + ".json.gz")
        url = "https://query2.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(sym.replace(".", "-")) + "?" + urllib.parse.urlencode(
            {"interval": "1d", "period1": 0, "period2": finish, "events": "splits,div", "includePrePost": "false"})
        record = {"ticker": sym, "url": url, "status": "unavailable", "attempts": []}
        try:
            if filename.exists():
                body = gzip.decompress(filename.read_bytes())
            else:
                body = None
                for attempt in range(2):
                    try:
                        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(request, timeout=30) as response:
                            body = response.read()
                        break
                    except urllib.error.HTTPError as exc:
                        error = exc.read().decode(errors="replace")[:700]
                        record["attempts"].append({"code": exc.code, "body": error})
                        if exc.code == 429 and attempt == 0:
                            time.sleep(min(30, max(5, int(exc.headers.get("Retry-After", "5")))))
                        else:
                            raise
                filename.write_bytes(gzip.compress(body, mtime=0))
                time.sleep(0.3)
            result = validate_response(body, sym)
            record.update(status="loaded", raw_sha256=hashlib.sha256(body).hexdigest(),
                rows=len(result["timestamp"]), first_timestamp=result["timestamp"][0], last_timestamp=result["timestamp"][-1],
                raw_file=str(filename.relative_to(output)), granularity=result["meta"]["dataGranularity"])
        except (urllib.error.URLError, ValueError, KeyError, TypeError, IndexError, OSError) as exc:
            record.update(error=str(exc), error_type=type(exc).__name__)
        return record

    records = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, record in enumerate(pool.map(fetch, universe + ["SPY", "QQQ"]), 1):
            records.append(record)
            if i % 100 == 0:
                write(output / "acquisition-progress.json", {"completed": i, "requested": len(universe) + 2,
                    "loaded": sum(r["status"] == "loaded" for r in records)})
                print("Daily acquisition", i, "/", len(universe) + 2, "loaded", sum(r["status"] == "loaded" for r in records), flush=True)
    manifest = {"frozen_sha256": digest(frozen), "records": records,
                "flags": ["SURVIVORSHIP_BIAS_RISK", "REVISED_DATA_NOT_POINT_IN_TIME", "CURRENT_DIRECTORY_NOT_HISTORICAL_MEMBERSHIP"],
                "intraday_requested": False}
    write(output / "acquisition.json", manifest)
    print("Daily acquisition complete", sum(r["status"] == "loaded" for r in records), "/", len(records), flush=True)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--classification", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--end", default="2026-11-06")
    args = parser.parse_args()
    collect(read(args.classification), args.output, args.end, args.workers)
