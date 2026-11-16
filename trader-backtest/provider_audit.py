#!/usr/bin/env python3
"""Small, explicit provider capability probes. Never reconstructs strategies."""
import argparse
import datetime as dt
from pathlib import Path

from common import Clock, calendar_2024_2027, iso, read, stamp, write
from intraday_providers import AlpacaProvider, BarCache, BarRequest, MassiveProvider, YahooProvider

CLOCK = Clock(calendar_2024_2027())
HISTORY_DATES = {2026: "2026-06-15", 2025: "2025-06-16", 2024: "2024-06-17",
                 2022: "2022-06-15", 2020: "2020-06-15", 2018: "2018-06-15", 2016: "2016-06-15"}
STOP = {"AUTH_ERROR", "RATE_LIMITED", "PROVIDER_ERROR"}


def last_completed_session(now):
    now = stamp(now) - dt.timedelta(minutes=15)  # Basic's recent-data delay.
    if not "2024-01-01" <= now.date().isoformat() <= "2027-12-31":
        raise ValueError("extend the supplied exchange calendar before live auditing")
    days = [d for d in CLOCK.sessions if CLOCK.close(d) <= now]
    if not days:
        raise ValueError("no completed exchange session in calendar")
    return days[-1]


def window(ticker, frame, day, local_start="09:30", minutes=30, adjustment="raw"):
    start = CLOCK.eastern_to_utc(dt.datetime.combine(dt.date.fromisoformat(day), dt.time.fromisoformat(local_start)))
    return BarRequest(ticker, frame, iso(start), iso(start + dt.timedelta(minutes=minutes)), adjustment)


def probe(provider, request, kind):
    fetched = provider.fetch_bars(request)
    bars = fetched["bars"]
    return dict(kind=kind, provider=provider.name, feed=provider.feed, request=fetched["request"],
        status=fetched["status"], bars=len(bars), pages=fetched["pages"], cache_hit=fetched["cache_hit"],
        http_events=fetched["http_events"], first=bars[0]["timestamp_utc"] if bars else None,
        archived_http_events=fetched["archived_http_events"],
        last=bars[-1]["timestamp_utc"] if bars else None,
        sessions=sorted({b["session"] for b in bars}),
        session_calendar_status=sorted({b["session_calendar_status"] for b in bars}),
        feed_verified=all(b["provider"] == provider.name and b["feed"] == provider.feed for b in bars),
        quality=fetched["quality"])


def alpaca_free_test(provider, day, output):
    """Always IEX, independent of a user's paid-feed routing preference."""
    if not provider.authenticated:
        return dict(status="SKIPPED_MISSING_AUTH", feed="IEX", probes=[])
    if provider.feed != "IEX":
        raise ValueError("free test must use IEX")
    prior_year = str(int(day[:4]) - 1) + day[4:]
    older = max(d for d in CLOCK.sessions if d <= prior_year)
    probes = []
    for date in (day, older):
        for ticker in ("AAPL", "MSFT", "NVDA"):
            for frame in ("1m", "5m"):
                record = probe(provider, window(ticker, frame, date), "FREE_IEX_REGULAR")
                probes.append(record)
                if record["status"] in STOP:
                    return dict(status="FAILED", feed="IEX", probes=probes, stopped_on=record["status"])
    for local_start, kind in (("08:00", "PREMARKET"), ("16:00", "AFTERHOURS")):
        record = probe(provider, window("AAPL", "1m", day, local_start, 15), kind)
        probes.append(record)
        if record["status"] in STOP:
            return dict(status="FAILED", feed="IEX", probes=probes, stopped_on=record["status"])
    # Separate cache: a prior large-page response must not bypass this live
    # next_page_token exercise. 10/30 minutes, not a large history download.
    paging = AlpacaProvider(feed="iex", cache=BarCache(Path(output) / "pagination-cache"), page_size=2)
    for frame, minutes in (("1m", 10), ("5m", 30)):
        record = probe(paging, window("AAPL", frame, day, minutes=minutes), "FORCED_PAGINATION")
        probes.append(record)
        if record["status"] in STOP:
            break
    regular = [r for r in probes if r["kind"] == "FREE_IEX_REGULAR"]
    paged = [r for r in probes if r["kind"] == "FORCED_PAGINATION"]
    passed = (len(regular) == 12 and all(r["status"] == "AVAILABLE" and r["feed_verified"] and
        r["quality"]["status"] != "INVALID" for r in regular) and len(paged) == 2 and
        all(r["status"] == "AVAILABLE" and r["pages"] > 1 for r in paged))
    return dict(status="PASSED" if passed else "FAILED", feed="IEX", probes=probes,
                extended_sessions_observed={k: any(k in r["sessions"] for r in probes) for k in ("PREMARKET", "AFTERHOURS")})


def historical_test(provider, now):
    if not provider.authenticated:
        return dict(status="SKIPPED_MISSING_AUTH", years={}, old_tickers=[])
    years, old_tickers = {}, []
    for year, day in HISTORY_DATES.items():
        if stamp(window("AAPL", "1m", day).end) > stamp(now):
            years[str(year)] = dict(status="NOT_REQUESTED_FUTURE")
            continue
        record = probe(provider, window("AAPL", "1m", day, minutes=10), "HISTORY_YEAR_WINDOW")
        years[str(year)] = record
        if record["status"] in STOP:
            return dict(status="STOPPED", years=years, old_tickers=[], stopped_on=record["status"])
    for ticker, day in (("TWTR", "2022-06-15"), ("FB", "2022-06-01")):
        record = probe(provider, window(ticker, "1m", day, minutes=10), "DELISTED_OR_OLD_SYMBOL")
        old_tickers.append(record)
        if record["status"] in STOP:
            break
    return dict(status="COMPLETED", years=years, old_tickers=old_tickers,
                limitation="Small observed windows only; no year-wide coverage, identity or survivorship certification")


def audit(output, now=None, live_yahoo=False):
    output = Path(output)
    now = now or dt.datetime.now(dt.timezone.utc)
    day = last_completed_session(now)
    cache = BarCache(output / "cache")
    alpaca = AlpacaProvider(feed="iex", cache=cache)
    yahoo, massive = YahooProvider(cache=cache), MassiveProvider(cache=cache)
    # First network activity, when auth exists, is exclusively the free test.
    free = alpaca_free_test(alpaca, day, output)
    access_observed = any(p["status"] == "AVAILABLE" for p in free["probes"]) and not free.get("stopped_on")
    history = historical_test(alpaca, now) if access_observed else dict(status="SKIPPED_NO_LIVE_ACCESS", years={}, old_tickers=[])
    sip = AlpacaProvider(feed="sip", cache=cache)
    sip_probe = probe(sip, window("AAPL", "1m", day, minutes=1), "SIP_ENTITLEMENT_ONLY") if free["status"] == "PASSED" else dict(status="NOT_TESTED")
    sip_history = historical_test(sip, now) if sip_probe["status"] == "AVAILABLE" else dict(status="SKIPPED_SIP_NOT_AVAILABLE", years={})
    yahoo_probes = []
    if live_yahoo:
        for frame in ("1m", "5m", "15m", "60m"):
            record = probe(yahoo, window("AAPL", frame, day, minutes=60, adjustment="split"), "YAHOO_BOUNDED_CAPABILITY")
            yahoo_probes.append(record)
            if record["status"] in STOP:
                break
    massive_probe = (probe(massive, window("AAPL", "1m", day, minutes=10), "CAPABILITY_ONLY")
                     if massive.authenticated else dict(status="DATA_UNAVAILABLE", reason="MISSING_MASSIVE_API_KEY", requests_sent=0))
    result = dict(as_of=now.isoformat(), last_completed_session=day,
        providers=[p.capabilities() for p in (yahoo, alpaca, massive)],
        alpaca_free_test=free, alpaca_history=history, alpaca_sip=sip_probe, alpaca_sip_history=sip_history,
        yahoo_probes=yahoo_probes, massive_probe=massive_probe,
        interpretation="Earliest/latest are observed bars in small windows, never proven subscription-wide history boundaries",
        pilot_status="READY_FOR_EXPLICIT_PILOT" if free["status"] == "PASSED" else "NOT_RUN_ALPACA_LIVE_TEST_REQUIRED")
    write(output / "provider-capability-audit.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--live-yahoo", action="store_true")
    args = parser.parse_args()
    result = audit(args.output, live_yahoo=args.live_yahoo)
    for provider in result["providers"]:
        print(provider["provider"], provider["feed"], provider["status"], provider["earliest_available"], provider["latest_available"])
