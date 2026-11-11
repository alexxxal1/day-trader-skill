#!/usr/bin/env python3
"""Explicit network-only acquisition. Engine replay never downloads anything."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

from common import EPOCH, calendar_2024_2027, digest, iso, read, write
from common import Clock


def collect(config, folder):
    folder = Path(folder)
    raw_dir = folder / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    clock = Clock(calendar_2024_2027())
    doc = {"schema_version": 1, "sessions": clock.sessions, "universe": config["universe"],
           "bars": {}, "splits": {}, "evidence": {"events": [], "metadata": []},
           "provenance": {"provider": "Yahoo chart (unofficial)", "retrospective_snapshot": True,
                          "point_in_time_universe_includes_delisted": False,
                          "universe_selection": "Eight predeclared project/watchlist stocks; current survivors, no historical membership archive",
                          "price_basis": "YAHOO_SPLIT_ADJUSTED; dividends excluded",
                          "calendar_source": "NYSE 2024-2027 schedule; 2025-01-09 Carter mourning closure included",
                          "config_sha256_before_collection": digest(config),
                          "catalysts": "No historical publication/observation archive; none inferred",
                          "borrow": "No historical borrow availability/rates", "cfd": "No historical CFD quotes/costs",
                          "requests": [], "errors": []}}
    begin = int((dt.datetime(2024, 7, 1) - EPOCH).total_seconds())
    finish = int((dt.datetime.fromisoformat(config["end"]) + dt.timedelta(days=1) - EPOCH).total_seconds())
    for sym in config["universe"] + ["SPY", "QQQ"]:
        for frame in ("1d", "5m"):
            params = {"interval": frame, "events": "splits,div", "includePrePost": "false"}
            params.update({"period1": begin, "period2": finish} if frame == "1d" else {"range": "60d"})
            path = raw_dir / (sym + "-" + frame + ".json")
            url = "https://query2.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(sym) + "?" + urllib.parse.urlencode(params)
            try:
                if path.exists():
                    body = path.read_bytes()
                else:
                    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(request, timeout=30) as response:
                        body = response.read()
                    path.write_bytes(body)
                    time.sleep(0.5)
                result = json.loads(body)["chart"]["result"][0]
                if result.get("meta", {}).get("symbol", "").upper() != sym:
                    raise ValueError("provider symbol mismatch")
                q = result["indicators"]["quote"][0]
                doc["provenance"]["requests"].append({"symbol": sym, "interval": frame, "url": url,
                    "raw_sha256": hashlib.sha256(body).hexdigest(), "raw_file": str(path.relative_to(folder))})
                splits = (result.get("events") or {}).get("splits", {})
                doc["splits"].setdefault(sym, {}).update(splits)
                rows = []
                for i, timestamp in enumerate(result["timestamp"]):
                    start = EPOCH + dt.timedelta(seconds=timestamp)
                    local = clock.to_eastern(start)
                    day = local.date()
                    hours = clock.session(day)
                    if not hours or day.isoformat() > config["end"] or not hours[0] <= local.time() < hours[1]:
                        continue
                    end = clock.close(day) if frame == "1d" else start + dt.timedelta(minutes=5)
                    values = {k: q[name][i] if i < len(q.get(name, [])) else None
                              for k, name in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume"))}
                    rows.append(dict(values, start=iso(start), end=iso(end), available_at=iso(end),
                                     availability_assumption="retrospective close-time proxy"))
                doc["bars"].setdefault(sym, {})[frame] = rows
                print(sym, frame, len(rows), flush=True)
            except (urllib.error.URLError, ValueError, KeyError, TypeError, IndexError) as exc:
                doc["provenance"]["errors"].append({"symbol": sym, "interval": frame, "error": str(exc)})
                print(sym, frame, "UNAVAILABLE", type(exc).__name__, flush=True)
    write(folder / "dataset.json", doc)
    write(folder / "collection-config.json", config)
    return doc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    collect(read(args.config), args.output)
