#!/usr/bin/env python3
"""Bounded source audit. No paging around retention limits, no endpoint rotation."""
import argparse
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

from common import Clock, read, write

HOST = "https://query2.finance.yahoo.com/v8/finance/chart/"
CLOCK = Clock({})
UA = "Mozilla/5.0 (compatible; day-trader historical research)"


def request_chart(symbol, params, path):
    """Save success AND error bodies; never retry/rotate after a rate limit."""
    path = Path(path)
    url = HOST + urllib.parse.quote(symbol.replace(".", "-"), safe="") + "?" + urllib.parse.urlencode(params)
    record_path = path.with_suffix(".request.json")
    if path.exists() and record_path.exists():
        record = read(record_path)
        if record["url"] != url:
            raise ValueError("immutable request differs; choose a new directory")
        body = gzip.decompress(path.read_bytes())
        if hashlib.sha256(body).hexdigest() != record["raw_sha256"]:
            raise ValueError("raw response hash mismatch")
        return record, body
    if path.exists() or record_path.exists():
        raise ValueError("incomplete immutable response archive; choose a new directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"url": url, "requested_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read()
            record.update(http_status=response.status, retry_after=response.headers.get("Retry-After"))
    except urllib.error.HTTPError as exc:
        body = exc.read()
        record.update(http_status=exc.code, retry_after=exc.headers.get("Retry-After"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        body = b""
        record.update(http_status=None, error=str(exc))
    record.update(raw_sha256=hashlib.sha256(body).hexdigest(), raw_file=str(path))
    path.write_bytes(gzip.compress(body, mtime=0))
    write(record_path, record)
    return record, body


def describe(body, interval):
    try:
        chart = json.loads(body)["chart"]
        if chart.get("error"):
            return {"status": "UNAVAILABLE", "provider_error": chart["error"]}
        result = chart["result"][0]
        stamps = result.get("timestamp") or []
        meta = result["meta"]
        times = [CLOCK.ts_to_eastern(t) for t in stamps]
        return {"status": "AVAILABLE" if stamps else "EMPTY", "bars": len(stamps),
                "first_utc": dt.datetime.fromtimestamp(stamps[0], dt.timezone.utc).isoformat() if stamps else None,
                "last_utc": dt.datetime.fromtimestamp(stamps[-1], dt.timezone.utc).isoformat() if stamps else None,
                "granularity": meta.get("dataGranularity"), "requested_granularity": interval,
                "exchange_timezone": meta.get("exchangeTimezoneName"),
                "premarket_observed": any(t.time() < dt.time(9, 30) for t in times),
                "afterhours_observed": any(t.time() >= dt.time(16) for t in times),
                "duplicate_timestamps": len(stamps) - len(set(stamps)),
                "monotonic_timestamps": stamps == sorted(stamps)}
    except (ValueError, KeyError, IndexError, TypeError):
        return {"status": "UNAVAILABLE", "provider_error": body.decode(errors="replace")[:1000]}


def audit(output, now=None):
    output = Path(output)
    if (output / "coverage-audit.json").exists():
        return read(output / "coverage-audit.json")
    now = now or dt.datetime.now(dt.timezone.utc)
    # One recent and one deliberately over-limit request per interval. These
    # are diagnostics, not attempts to retrieve old history in short chunks.
    cases = [("1m", "7d"), ("1m", "30d"), ("5m", "60d"), ("5m", "90d"),
             ("15m", "60d"), ("15m", "90d"), ("60m", "730d"), ("60m", "3y")]
    probes = []
    for interval, span in cases:
        record, body = request_chart("AAPL", {"interval": interval, "range": span,
            "includePrePost": "true", "events": "splits,div"}, output / "raw" / (interval + "-" + span + ".json.gz"))
        probe = dict(record, interval=interval, requested_range=span, **describe(body, interval))
        probes.append(probe)
        print(interval, span, probe["status"], probe.get("first_utc"), probe.get("provider_error", ""), flush=True)
        if record["http_status"] in (429, 401, 403) or record["http_status"] is None:
            break
        time.sleep(0.5)
    result = {"audited_at": now.isoformat(), "status": "INTRADAY_HISTORY_LIMITED",
        "scope": "Current permitted sources/adapters; observed responses are not a retention SLA",
        "yahoo": {"probes": probes, "split_adjusted": "YES: Yahoo quote OHLC, not dividend-adjusted adjclose; revisable snapshot",
            "timestamp_quality": "UTC epoch interval starts plus America/New_York; incomplete last bars must be excluded; no historical delivery vintages",
            "rate_limits": "Undocumented/dynamic. Serial requests, 0.5s pause; stop on 429/auth failure, no rotation or bypass.",
            "historical_certification": "NOT_HISTORICALLY_PIT_CERTIFIED"},
        "forbidden_inference": "No 1993–2026 intraday claim; 15m/60m cannot resolve 1m/5m triggers; no daily fallback"}
    result["other_sources"] = [
        {"source": "Stooq", "bars_1_5_15_60m": "NOT_AVAILABLE_IN_EXISTING_ADAPTERS", "maximum_history": "No validated US intraday adapter in repository", "premarket": None, "afterhours": None, "split_adjusted": None, "timestamp_quality": "UNVERIFIED", "rate_limits": "UNKNOWN; no unsupported API probing"},
        *[{"source": name, "bars_1_5_15_60m": "NO_OHLCV_SOURCE", "maximum_history": "N/A for intraday prices", "premarket": None, "afterhours": None, "split_adjusted": None, "timestamp_quality": quality, "rate_limits": limit}
          for name, quality, limit in [
              ("Nasdaq symbol directory", "Directory snapshot, no price bars", "Undocumented; two cached directory files"),
              ("Yahoo RSS / trader-news", "Recent publication timestamps; no archived historical availability/catalyst verification", "Undocumented/dynamic"),
              ("SEC EDGAR / trader-news", "Filing acceptance can timestamp evidence; no automatic confirmation of economic catalyst", "SEC fair access <=10 requests/s; not used for price bars"),
              ("trader-social", "No configured historical social archive/credentials", "Provider-dependent; unavailable"),
              ("Quiver public pages / House disclosures", "Disclosure/publication differs from transaction date; no archived observed catalyst feed", "Undocumented; Quiver API not configured/allowed")]]]
    write(output / "coverage-audit.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    audit(parser.parse_args().output)
