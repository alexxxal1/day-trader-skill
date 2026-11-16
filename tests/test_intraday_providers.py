"""Offline REST contracts, failure recovery, provenance and timezone boundaries."""
import copy
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from _load import ROOT
sys.path.insert(0, str(Path(ROOT) / "trader-backtest"))
from common import Clock, calendar_2024_2027, digest, iso, read, stamp
from data import MarketData
from engine import cost_scenarios
from intraday_providers import (AlpacaProvider, BarCache, BarRequest, IntradayProvider,
    MassiveProvider, ProviderRouter, YahooProvider, execution_row, normalize_bar, validate_bars)
from intraday_providers.cache import atomic_write
from intraday_providers.provenance import source_identity
from intraday_providers.registry import configured
from intraday_providers.transport import HTTPClient, NoRedirect, ProviderError
from provider_audit import alpaca_free_test, historical_test, last_completed_session, window
from provider_collect import collect
from provider_pilot import comparison, pilot
from true_trade_execution import reconstruct

START, END = "2026-11-09T14:30:00Z", "2026-11-09T15:00:00Z"
CLOCK = Clock(calendar_2024_2027())
CFG = read(Path(ROOT) / "trader-backtest/true-trade-config.json")
AUTH = {"ALPACA_API_KEY": "offline-fixture-key", "ALPACA_API_SECRET": "offline-fixture-secret", "MASSIVE_API_KEY": "offline-fixture-massive"}


def request(frame="1m", adjustment="raw", start=START, end=END):
    return BarRequest("AAPL", frame, start, end, adjustment)


def alpaca_bar(at=START):
    return dict(t=at, o=100, h=102, l=99, c=101, v=1000, vw=100.5, n=20)


def massive_bar(at=START):
    row = alpaca_bar(at)
    row["t"] = int(stamp(at).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    return row


def normal(at=START, provider="ALPACA", feed="IEX", req=None):
    return normalize_bar(req or request(), at, alpaca_bar(at), provider, feed)


class Transport:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, params, headers):
        self.calls.append((url, dict(params), dict(headers)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def ok(body):
    return 200, {}, body


def client(host, responses, **kwargs):
    transport, sleeps = Transport(responses), []
    result = HTTPClient(host, transport=transport, sleep=sleeps.append, monotonic=lambda: 0, **kwargs)
    result.fixture, result.sleeps = transport, sleeps
    return result


def alpaca(responses, **kwargs):
    http = client("data.alpaca.markets", responses)
    with patch.dict("os.environ", AUTH, clear=True):
        return AlpacaProvider(client=http, **kwargs)


def massive(responses, **kwargs):
    http = client("api.massive.com", responses)
    with patch.dict("os.environ", AUTH, clear=True):
        return MassiveProvider(client=http, **kwargs)


def document(symbols=("AAPL",)):
    doc = dict(sessions={"2026-11-09": ["09:30", "16:00"]}, universe=list(symbols),
        bars={s: {"1d": [], "1m": [], "5m": []} for s in symbols}, splits={}, evidence={"events": [], "metadata": []},
        provenance={"retrospective_snapshot": True})
    return certify_document(doc)


def certify_document(doc):
    doc["provenance"]["daily_price_basis"] = {s: dict(adjustment="split", anchor_date="2026-11-13",
        complete_from="2024-01-01", complete_through="2026-11-13", evidence_id="offline-archived-context",
        bars_sha256=digest(doc["bars"][s]["1d"]), splits_sha256=digest(doc.get("splits", {}).get(s, {})))
        for s in doc["universe"]}
    return doc


class InterfaceTests(unittest.TestCase):
    def test_sixty_minute_alias_uses_the_same_cache_identity(self):
        self.assertEqual(BarRequest("AAPL", "60Min", START, END), request("60m"))

    def test_subsecond_request_boundary_is_not_silently_truncated(self):
        with self.assertRaisesRegex(ValueError, "whole-second"):
            request(start="2026-11-09T14:30:00.001Z")

    def test_abstract_contract(self):
        with self.assertRaises(TypeError):
            IntradayProvider(None)

    def test_all_providers_support_four_native_timeframes(self):
        for provider in (alpaca([]), massive([]), YahooProvider()):
            self.assertEqual(provider.supported_timeframes(), ("1m", "5m", "15m", "60m"))

    def test_request_canonicalizes_timezone_and_symbol(self):
        r = BarRequest("aapl", "1m", "2026-11-09T09:30:00-05:00", "2026-11-09T10:00:00-05:00")
        self.assertEqual(r, request())

    def test_request_rejects_naive_timestamp(self):
        with self.assertRaises(ValueError):
            request(start="2026-11-09T09:30:00")

    def test_request_rejects_invalid_inputs(self):
        for args in (("AAPL", "1d", START, END), ("../AAPL", "1m", START, END), ("AAPL", "1m", END, START)):
            with self.assertRaises(ValueError):
                BarRequest(*args)

    def test_no_invented_history_before_observation(self):
        caps = alpaca([]).capabilities()
        self.assertIsNone(caps["available"])
        self.assertIsNone(caps["earliest_available"])
        self.assertEqual(caps["history_depth"], "UNKNOWN_UNTESTED")

    def test_coverage_reports_only_observed_window(self):
        p = alpaca([ok(dict(bars=[alpaca_bar()]))])
        self.assertEqual(p.coverage(request())["status"], "NOT_TESTED")
        p.fetch_bars(request())
        self.assertEqual(p.coverage(request())["first"], START)
        self.assertEqual(p.capabilities()["earliest_available"], START)

    def test_normalized_schema_optional_fields(self):
        row = normalize_bar(request(), START, dict(o=1, h=1, l=1, c=1, v=1), "ALPACA", "IEX")
        self.assertIsNone(row["vwap"])
        self.assertIsNone(row["trade_count"])
        self.assertEqual(row["timezone"], "America/New_York")
        self.assertEqual(row["adjustment"], "raw")


class AlpacaTests(unittest.TestCase):
    def test_auth_failure_halts_provider_for_remaining_acquisition(self):
        p = alpaca([(401, {}, {})])
        p.fetch_bars(request())
        self.assertEqual(p.fetch_bars(request("5m"))["status"], "AUTH_ERROR")
        self.assertEqual(len(p.client.fixture.calls), 1)

    def test_official_market_data_path_and_auth_headers(self):
        p = alpaca([ok(dict(bars=[alpaca_bar()]))])
        r = p.fetch_bars(request())
        url, params, headers = p.client.fixture.calls[0]
        self.assertEqual(url, "https://data.alpaca.markets/v2/stocks/AAPL/bars")
        self.assertEqual(headers, {"APCA-API-KEY-ID": AUTH["ALPACA_API_KEY"], "APCA-API-SECRET-KEY": AUTH["ALPACA_API_SECRET"]})
        self.assertEqual(r["status"], "AVAILABLE")
        self.assertNotIn(AUTH["ALPACA_API_SECRET"], json.dumps(r))

    def test_iex_default_is_explicit_on_every_bar(self):
        p = alpaca([ok(dict(bars=[alpaca_bar()]))])
        r = p.fetch_bars(request())
        self.assertEqual(p.client.fixture.calls[0][1]["feed"], "iex")
        self.assertEqual((r["provider"], r["feed"], r["bars"][0]["feed"]), ("ALPACA", "IEX", "IEX"))

    def test_sip_is_distinct_and_explicit(self):
        p = alpaca([ok(dict(bars=[alpaca_bar()]))], feed="sip")
        r = p.fetch_bars(request())
        self.assertEqual(r["feed"], "SIP")
        self.assertEqual(p.client.fixture.calls[0][1]["feed"], "sip")

    def test_all_rest_timeframes_and_split_adjustment(self):
        for frame, expected in (("1m", "1Min"), ("5m", "5Min"), ("15m", "15Min"), ("60m", "1Hour")):
            p = alpaca([ok(dict(bars=[]))])
            p.fetch_bars(request(frame, "split"))
            params = p.client.fixture.calls[0][1]
            self.assertEqual((params["timeframe"], params["adjustment"], params["sort"]), (expected, "split", "asc"))

    def test_pagination_follows_every_next_page_token(self):
        p = alpaca([ok(dict(bars=[alpaca_bar()], next_page_token="page2")),
                    ok(dict(bars=[alpaca_bar("2026-11-09T14:31:00Z")], next_page_token="page3")),
                    ok(dict(bars=[alpaca_bar("2026-11-09T14:32:00Z")]))])
        r = p.fetch_bars(request())
        self.assertEqual((len(r["bars"]), r["pages"]), (3, 3))
        self.assertEqual([c[1].get("page_token") for c in p.client.fixture.calls], [None, "page2", "page3"])

    def test_empty_page_with_continuation_does_not_stop(self):
        p = alpaca([ok(dict(bars=[], next_page_token="next")), ok(dict(bars=[alpaca_bar()]))])
        self.assertEqual(p.fetch_bars(request())["status"], "AVAILABLE")

    def test_empty_response(self):
        self.assertEqual(alpaca([ok(dict(bars=None))]).fetch_bars(request())["status"], "EMPTY")

    def test_missing_bars_is_invalid_response(self):
        self.assertEqual(alpaca([ok({})]).fetch_bars(request())["status"], "INVALID_RESPONSE")

    def test_missing_one_secret_sends_no_request(self):
        for present in ("ALPACA_API_KEY", "ALPACA_API_SECRET"):
            http = client("data.alpaca.markets", [])
            with patch.dict("os.environ", {present: "fixture"}, clear=True):
                p = AlpacaProvider(client=http)
            self.assertEqual(p.fetch_bars(request())["status"], "DATA_UNAVAILABLE")
            self.assertEqual(http.fixture.calls, [])

    def test_429_pause_then_retry(self):
        p = alpaca([(429, {"Retry-After": "3"}, {}), ok(dict(bars=[alpaca_bar()]))])
        self.assertEqual(p.fetch_bars(request())["status"], "AVAILABLE")
        self.assertIn(3, p.client.sleeps)
        self.assertEqual(len(p.client.fixture.calls), 2)

    def test_401_and_403_abort_without_retry(self):
        for code in (401, 403):
            p = alpaca([(code, {}, {})])
            self.assertEqual(p.fetch_bars(request())["status"], "AUTH_ERROR")
            self.assertEqual(len(p.client.fixture.calls), 1)

    def test_repeated_cursor_fails_without_exposing_partial_data(self):
        p = alpaca([ok(dict(bars=[alpaca_bar()], next_page_token="same")),
                    ok(dict(bars=[alpaca_bar()], next_page_token="same"))])
        r = p.fetch_bars(request())
        self.assertEqual(r["status"], "PAGINATION_LOOP")
        self.assertEqual(r["bars"], [])


class MassiveTests(unittest.TestCase):
    def test_missing_key_sends_no_request(self):
        http = client("api.massive.com", [])
        with patch.dict("os.environ", {}, clear=True):
            p = MassiveProvider(client=http)
        self.assertEqual(p.fetch_bars(request())["status"], "DATA_UNAVAILABLE")
        self.assertEqual(http.fixture.calls, [])

    def test_official_aggregate_parsing_and_auth(self):
        p = massive([ok(dict(status="OK", ticker="AAPL", adjusted=False, results=[massive_bar()]))])
        r = p.fetch_bars(request())
        url, params, headers = p.client.fixture.calls[0]
        self.assertIn("https://api.massive.com/v2/aggs/ticker/AAPL/range/1/minute/", url)
        self.assertEqual(headers, {"Authorization": "Bearer " + AUTH["MASSIVE_API_KEY"]})
        self.assertEqual(params["adjusted"], "false")
        self.assertEqual(r["bars"][0]["timestamp_utc"], START)
        self.assertEqual(r["bars"][0]["vwap"], 100.5)

    def test_multipliers_and_adjusted_true(self):
        for frame, multiplier in (("1m", 1), ("5m", 5), ("15m", 15), ("60m", 60)):
            p = massive([ok(dict(status="OK", adjusted=True, results=[]))])
            p.fetch_bars(request(frame, "split"))
            self.assertIn("/range/%d/minute/" % multiplier, p.client.fixture.calls[0][0])
            self.assertEqual(p.client.fixture.calls[0][1]["adjusted"], "true")

    def test_pagination_and_query_secret_is_removed(self):
        p = massive([ok(dict(status="OK", results=[massive_bar()],
            next_url="https://api.massive.com/v2/aggs/ticker/AAPL?cursor=next&apiKey=DO_NOT_ARCHIVE")),
            ok(dict(status="OK", results=[massive_bar("2026-11-09T14:31:00Z")]))])
        with tempfile.TemporaryDirectory() as folder:
            p.cache = BarCache(folder)
            r = p.fetch_bars(request())
            self.assertEqual((r["status"], r["pages"]), ("AVAILABLE", 2))
            self.assertNotIn("apiKey", p.client.fixture.calls[1][0])
            self.assertNotIn("DO_NOT_ARCHIVE", "".join(f.read_text() for f in Path(folder).rglob("*.json")))

    def test_pagination_cannot_exfiltrate_auth(self):
        p = massive([ok(dict(status="OK", results=[], next_url="https://other.example/v2/aggs"))])
        self.assertEqual(p.fetch_bars(request())["status"], "UNSAFE_PAGINATION_URL")
        self.assertEqual(len(p.client.fixture.calls), 1)

    def test_adjustment_mismatch_is_rejected(self):
        p = massive([ok(dict(status="OK", adjusted=True, results=[massive_bar()]))])
        self.assertEqual(p.fetch_bars(request())["status"], "ADJUSTMENT_MISMATCH")

    def test_provider_error_in_200_response_is_rejected(self):
        p = massive([ok(dict(status="NOT_AUTHORIZED", error="fixture"))])
        self.assertEqual(p.fetch_bars(request())["status"], "PROVIDER_ERROR")

    def test_reference_interfaces_preserve_as_of_and_active_flag(self):
        p = massive([ok(dict(status="OK", results=[])) for _ in range(5)])
        p.tickers(True, "2020-06-15")
        p.tickers(False, "2020-06-15")
        p.ticker_details("TWTR", "2020-06-15")
        p.splits("AAPL")
        p.ticker_events("META")
        calls = p.client.fixture.calls
        self.assertEqual([calls[0][1]["active"], calls[1][1]["active"]], ["true", "false"])
        self.assertEqual(calls[2][1]["date"], "2020-06-15")
        self.assertTrue(calls[3][0].endswith("/stocks/v1/splits"))
        self.assertTrue(calls[4][0].endswith("/vX/reference/tickers/META/events"))

    def test_no_key_also_disables_all_reference_calls(self):
        with patch.dict("os.environ", {}, clear=True):
            p = MassiveProvider(client=client("api.massive.com", []))
        results = [p.tickers(), p.tickers(False), p.ticker_details("AAPL", "2020-01-01"), p.splits("AAPL"), p.ticker_events("AAPL")]
        self.assertTrue(all(r["status"] == "DATA_UNAVAILABLE" for r in results))
        self.assertEqual(p.client.fixture.calls, [])


class YahooTests(unittest.TestCase):
    def provider(self, code=200):
        self.calls = []
        def fetch(ticker, params, path):
            self.calls.append((ticker, params, path))
            quotes = dict(open=[100], high=[102], low=[99], close=[101], volume=[1000])
            return dict(http_status=code), json.dumps(dict(chart=dict(result=[dict(
                meta=dict(symbol=ticker, dataGranularity=params["interval"]),
                timestamp=[1794234600], indicators=dict(quote=[quotes]))], error=None))).encode()
        return YahooProvider(requester=fetch, now=lambda: "2026-11-13T10:00:00Z")

    def test_legacy_request_chart_adapter_is_retained(self):
        p = self.provider()
        r = p.fetch_bars(request(adjustment="split"))
        self.assertEqual(r["status"], "AVAILABLE")
        self.assertEqual(r["history_status"], "INTRADAY_HISTORY_LIMITED")
        self.assertEqual(self.calls[0][1]["includePrePost"], "true")

    def test_retention_guard_sends_no_request(self):
        p = self.provider()
        r = p.fetch_bars(request(adjustment="split", start="2020-01-01T14:30:00Z", end="2020-01-01T15:00:00Z"))
        self.assertEqual(r["status"], "INTRADAY_HISTORY_LIMITED")
        self.assertEqual(self.calls, [])

    def test_raw_prices_not_mislabeled(self):
        p = self.provider()
        self.assertEqual(p.fetch_bars(request())["status"], "UNSUPPORTED_ADJUSTMENT")
        self.assertEqual(self.calls, [])

    def test_yahoo_429_keeps_legacy_stop_policy(self):
        p = self.provider(429)
        self.assertEqual(p.fetch_bars(request(adjustment="split"))["status"], "RATE_LIMITED")
        self.assertEqual(len(self.calls), 1)


class TimezoneTests(unittest.TestCase):
    def test_utc_to_winter_new_york_and_roundtrip(self):
        row = normal()
        self.assertEqual(row["timestamp_new_york"], "2026-11-09T09:30:00-05:00")
        self.assertEqual(stamp(row["timestamp_new_york"]), stamp(START))

    def test_summer_daylight_time(self):
        self.assertEqual(normal("2026-07-01T13:30:00Z")["timestamp_new_york"], "2026-07-01T09:30:00-04:00")

    def test_dst_fall_back_ambiguous_local_time_keeps_offset(self):
        first = normal("2026-11-01T05:30:00Z")
        second = normal("2026-11-01T06:30:00Z")
        self.assertEqual(first["timestamp_new_york"], "2026-11-01T01:30:00-04:00")
        self.assertEqual(second["timestamp_new_york"], "2026-11-01T01:30:00-05:00")

    def test_premarket(self):
        self.assertEqual(normal("2026-11-09T13:00:00Z")["session"], "PREMARKET")

    def test_regular_open(self):
        self.assertEqual(normal()["session"], "REGULAR")

    def test_afterhours_at_regular_close(self):
        self.assertEqual(normal("2026-11-09T21:00:00Z")["session"], "AFTERHOURS")

    def test_early_close(self):
        self.assertEqual(normal("2026-11-27T18:00:00Z")["session"], "AFTERHOURS")

    def test_exchange_holiday(self):
        self.assertEqual(normal("2026-11-26T15:00:00Z")["session"], "CLOSED")

    def test_older_history_marks_calendar_unverified(self):
        self.assertEqual(normal("2016-06-15T13:30:00Z")["session_calendar_status"], "CLOCK_ONLY_UNVERIFIED")

    def test_replay_availability_begins_at_bar_close(self):
        row = execution_row(normal())
        self.assertEqual(row["available_at"], "2026-11-09T14:31:00Z")
        self.assertEqual(row["end"], row["available_at"])


class ValidationTests(unittest.TestCase):
    def test_invalid_later_prices_become_holes_without_erasing_prior_bars(self):
        bad = dict(alpaca_bar("2026-11-09T14:31:00Z"), v=-1)
        p = alpaca([ok(dict(bars=[alpaca_bar(), bad]))])
        result = p.fetch_bars(request())
        self.assertEqual(result["status"], "AVAILABLE")
        self.assertEqual(result["quality"]["status"], "INVALID")
        self.assertEqual([b["timestamp_utc"] for b in result["bars"]], [START])
        self.assertEqual(len(result["quarantined_bars"]), 1)

    def test_future_invalid_price_does_not_drive_provider_fallback(self):
        bad = dict(alpaca_bar("2026-11-09T14:31:00Z"), h=90)
        a, y = alpaca([ok(dict(bars=[alpaca_bar(), bad]))]), StubProvider("YAHOO")
        selected = ProviderRouter([a, y], ["ALPACA", "YAHOO"]).fetch_trade([request()], opening=START)
        self.assertEqual(selected["provider"], "ALPACA")
        self.assertEqual(y.calls, [])

    def test_structural_duplicate_is_quarantined_without_erasing_prior_observation(self):
        p = alpaca([ok(dict(bars=[alpaca_bar(), alpaca_bar()]))])
        result = p.fetch_bars(request())
        self.assertEqual(result["status"], "AVAILABLE")
        self.assertEqual(len(result["bars"]), 1)
        self.assertEqual(result["quality"]["status"], "INVALID")

    def codes(self, rows, **kwargs):
        result = validate_bars(rows, request(**kwargs))
        return result, {r["code"] for r in result["errors"] + result["warnings"]}

    def test_valid_ohlcv(self):
        self.assertEqual(self.codes([normal()])[0]["status"], "VALID")

    def test_unsorted_timestamps(self):
        self.assertIn("UNSORTED_TIMESTAMPS", self.codes([normal("2026-11-09T14:31:00Z"), normal()])[1])

    def test_duplicate_timestamps(self):
        self.assertIn("DUPLICATE_TIMESTAMP", self.codes([normal(), normal()])[1])

    def test_ohlc_inconsistency(self):
        self.assertIn("INCONSISTENT_OHLC", self.codes([dict(normal(), high=90)])[1])

    def test_negative_volume(self):
        self.assertIn("INVALID_OHLCV", self.codes([dict(normal(), volume=-1)])[1])

    def test_missing_bars_are_reported_without_interpolation(self):
        rows = [normal(), normal("2026-11-09T14:33:00Z")]
        result, codes = self.codes(rows)
        self.assertIn("MISSING_BAR_SLOTS", codes)
        self.assertFalse(result["interpolated"])
        self.assertEqual(len(rows), 2)

    def test_extreme_jump_is_a_diagnostic_not_retrospective_pit_filter(self):
        row = dict(normal("2026-11-09T14:31:00Z"), open=300, high=302, low=299, close=301)
        result, codes = self.codes([normal(), row])
        self.assertIn("EXTREME_JUMP", codes)
        self.assertEqual(result["status"], "SUSPECT")

    def test_wrong_new_york_offset_is_rejected(self):
        self.assertIn("TIMEZONE_ERROR", self.codes([dict(normal(), timestamp_new_york="2026-11-09T09:30:00-04:00")])[1])

    def test_nan_prices_are_invalid(self):
        self.assertIn("INVALID_OHLCV", self.codes([dict(normal(), close=float("nan"))])[1])

    def test_missing_session_edges(self):
        _, codes = self.codes([normal()], end="2026-11-09T21:00:00Z")
        self.assertIn("MISSING_SESSION_EDGES", codes)


class CacheTests(unittest.TestCase):
    def test_retry_after_is_persisted_across_process_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            p = alpaca([(429, {"Retry-After": "120"}, {})], cache=BarCache(folder))
            self.assertEqual(p.fetch_bars(request())["status"], "RATE_LIMITED")
            resumed = alpaca([], cache=BarCache(folder))
            self.assertEqual(resumed.fetch_bars(request())["status"], "RATE_LIMITED")
            self.assertEqual(resumed.client.fixture.calls, [])

    def test_cache_miss_then_hit_without_second_http_call(self):
        with tempfile.TemporaryDirectory() as folder:
            p = alpaca([ok(dict(bars=[alpaca_bar()]))], cache=BarCache(folder))
            first, second = p.fetch_bars(request()), p.fetch_bars(request())
            self.assertFalse(first["cache_hit"])
            self.assertTrue(second["cache_hit"])
            self.assertEqual(first["bars"], second["bars"])
            self.assertEqual(len(p.client.fixture.calls), 1)

    def test_key_separates_every_required_dimension(self):
        cache = BarCache("unused")
        base = request().key("ALPACA", "IEX")
        for field, value in dict(provider="MASSIVE", feed="SIP", ticker="MSFT", timeframe="5m", start="other", end="other", adjustment="split").items():
            self.assertNotEqual(cache.directory(base), cache.directory(dict(base, **{field: value})))

    def test_resume_starts_at_failed_page(self):
        with tempfile.TemporaryDirectory() as folder:
            p = alpaca([ok(dict(bars=[alpaca_bar()], next_page_token="next")), (503, {}, {}),
                        ok(dict(bars=[alpaca_bar("2026-11-09T14:31:00Z")]))], cache=BarCache(folder))
            p.client.retries = 0
            first = p.fetch_bars(request())
            self.assertEqual((first["status"], first["partial_bars"], first["bars"]), ("PROVIDER_ERROR", 1, []))
            second = p.fetch_bars(request())
            self.assertEqual((second["status"], len(second["bars"])), ("AVAILABLE", 2))
            self.assertEqual([c[1].get("page_token") for c in p.client.fixture.calls], [None, "next", "next"])
            state, rows = p.cache.load(request().key(p.name, p.feed))
            self.assertTrue(state["complete"])
            self.assertEqual(len(state["errors"]), 1)

    def test_resume_after_last_page_before_finalization(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = BarCache(folder)
            key = request().key("ALPACA", "IEX")
            state, _ = cache.load(key)
            cache.page(key, state, [normal()], None)
            p = alpaca([], cache=cache)
            self.assertEqual(p.fetch_bars(request())["status"], "AVAILABLE")
            self.assertEqual(p.client.fixture.calls, [])

    def test_cache_corruption_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            p = alpaca([ok(dict(bars=[alpaca_bar()]))], cache=BarCache(folder))
            p.fetch_bars(request())
            page = next(Path(folder).rglob("page-*.json"))
            page.write_text("[]")
            with self.assertRaisesRegex(ValueError, "checksum"):
                p.fetch_bars(request())

    def test_empty_response_is_reproducibly_cached(self):
        with tempfile.TemporaryDirectory() as folder:
            p = alpaca([ok(dict(bars=[]))], cache=BarCache(folder))
            p.fetch_bars(request())
            self.assertTrue(p.fetch_bars(request())["cache_hit"])


class RetryTests(unittest.TestCase):
    def test_bounded_exponential_backoff_on_5xx(self):
        http = client("data.alpaca.markets", [(500, {}, {})] * 4)
        with self.assertRaises(ProviderError):
            http.get("https://data.alpaca.markets/v2/stocks/AAPL/bars", {}, {})
        self.assertEqual(len(http.fixture.calls), 4)
        self.assertEqual([s for s in http.sleeps if s >= 1], [1, 2, 4])

    def test_long_retry_after_checkpoints_instead_of_retrying_early(self):
        http = client("data.alpaca.markets", [(429, {"Retry-After": "120"}, {})])
        with self.assertRaises(ProviderError) as raised:
            http.get("https://data.alpaca.markets/v2/stocks/AAPL/bars", {}, {})
        self.assertEqual(raised.exception.status, "RATE_LIMITED")
        self.assertEqual(len(http.fixture.calls), 1)

    def test_network_error_is_retried_without_error_detail_leaks(self):
        p = alpaca([urllib.error.URLError("secret-bearing-url"), ok(dict(bars=[]))])
        result = p.fetch_bars(request())
        self.assertEqual(result["status"], "EMPTY")
        self.assertNotIn("secret-bearing-url", json.dumps(result))

    def test_other_4xx_is_not_retried(self):
        p = alpaca([(422, {}, {})])
        self.assertEqual(p.fetch_bars(request())["status"], "PROVIDER_ERROR")
        self.assertEqual(len(p.client.fixture.calls), 1)

    def test_success_with_invalid_json_is_reported(self):
        p = alpaca([(200, {}, b"not-json")])
        self.assertEqual(p.fetch_bars(request())["status"], "INVALID_RESPONSE")

    def test_redirects_never_forward_credentials(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))

    def test_cross_domain_or_http_urls_are_rejected_before_transport(self):
        http = client("api.massive.com", [])
        for url in ("http://api.massive.com/v2/aggs", "https://other.example/v2/aggs", "https://api.massive.com:443/v2/aggs"):
            with self.assertRaises(ProviderError):
                http.get(url, {}, {})
        self.assertEqual(http.fixture.calls, [])


class StubProvider:
    def __init__(self, name, status="AVAILABLE", by_frame=None, missing_open=False):
        self.name, self.feed, self.calls = name, "IEX" if name == "ALPACA" else "UNSPECIFIED", []
        self.status, self.by_frame, self.missing_open = status, by_frame or {}, missing_open

    def fetch_bars(self, req):
        self.calls.append(req)
        status = self.by_frame.get(req.timeframe, self.status)
        at = iso(stamp(req.start) + dt.timedelta(minutes=1)) if self.missing_open else req.start
        bars = [normal(at, self.name, self.feed, req)] if status == "AVAILABLE" else []
        return dict(provider=self.name, feed=self.feed, status=status, bars=bars, request=req.key(self.name, self.feed))


class RouterTests(unittest.TestCase):
    def test_default_priority_and_fallback(self):
        a, y, m = StubProvider("ALPACA", "DATA_UNAVAILABLE"), StubProvider("YAHOO"), StubProvider("MASSIVE")
        result = ProviderRouter([a, y, m]).fetch_trade([request()])
        self.assertEqual(result["provider"], "YAHOO")
        self.assertEqual(y.calls[0].adjustment, "split")
        self.assertEqual(m.calls, [])

    def test_massive_first_is_a_configuration_change(self):
        a, y, m = StubProvider("ALPACA"), StubProvider("YAHOO"), StubProvider("MASSIVE")
        result = ProviderRouter([a, y, m], ["MASSIVE", "ALPACA", "YAHOO"]).fetch_trade([request()])
        self.assertEqual(result["provider"], "MASSIVE")
        self.assertEqual(a.calls, [])

    def test_missing_secondary_frame_does_not_discard_a_valid_opening(self):
        a = StubProvider("ALPACA", by_frame={"5m": "PROVIDER_ERROR"})
        y = StubProvider("YAHOO")
        result = ProviderRouter([a, y], ["ALPACA", "YAHOO"]).fetch_trade([request(), request("5m")])
        self.assertEqual(result["provider"], "ALPACA")
        self.assertEqual({b["provider"] for r in result["results"] for b in r["bars"]}, {"ALPACA"})
        self.assertEqual(y.calls, [])

    def test_no_later_outcome_driven_switch(self):
        a, y = StubProvider("ALPACA"), StubProvider("YAHOO")
        result = ProviderRouter([a, y], ["ALPACA", "YAHOO"]).fetch_trade([request()], opening=START)
        self.assertEqual(result["provider"], "ALPACA")
        self.assertEqual(y.calls, [])  # A single opening bar is not a complete exit.

    def test_missing_opening_observation_can_fall_back(self):
        a, y = StubProvider("ALPACA", missing_open=True), StubProvider("YAHOO")
        result = ProviderRouter([a, y], ["ALPACA", "YAHOO"]).fetch_trade([request()], opening=START)
        self.assertEqual(result["provider"], "YAHOO")

    def test_no_auth_means_zero_real_http_requests_for_both_paid_adapters(self):
        with patch.dict("os.environ", {}, clear=True):
            a = AlpacaProvider(client=client("data.alpaca.markets", []))
            m = MassiveProvider(client=client("api.massive.com", []))
        result = ProviderRouter([a, m], ["ALPACA", "MASSIVE"]).fetch_trade([request()])
        self.assertEqual(result["status"], "DATA_UNAVAILABLE")
        self.assertEqual(a.client.fixture.calls + m.client.fixture.calls, [])

    def test_mixed_ticker_bundle_rejected(self):
        router = ProviderRouter([StubProvider("ALPACA")], ["ALPACA"])
        with self.assertRaises(ValueError):
            router.fetch_trade([request(), BarRequest("MSFT", "1m", START, END)])

    def test_duplicate_priority_rejected(self):
        with self.assertRaises(ValueError):
            ProviderRouter([StubProvider("ALPACA")], ["ALPACA", "ALPACA"])


class ProvenanceTests(unittest.TestCase):
    def execution(self, providers, feeds=None, plan_source=None):
        from test_true_trades import row, plan
        rows = [dict(row(2 + i, (105, 130, 104, 120) if i == len(providers) - 1 else (105, 106, 104, 105)), provider=p,
                     feed=(feeds or ["IEX"] * len(providers))[i], adjustment="raw") for i, p in enumerate(providers)]
        p = dict(plan(), **(plan_source or {}))
        return reconstruct(p, rows, CLOCK, CFG, cost_scenarios(CFG)["IDEAL"])

    def test_single_provider_true_trade_preserves_feed(self):
        result = self.execution(["ALPACA"])
        self.assertTrue(result["true_trade"])
        self.assertEqual((result["provider"], result["feed"]), ("ALPACA", "IEX"))

    def test_mixed_provider_never_produces_primary_true_trade(self):
        result = self.execution(["ALPACA", "YAHOO"])
        self.assertFalse(result["true_trade"])
        self.assertEqual(result["status"], "MIXED_PROVIDER_DATA")

    def test_mixed_iex_sip_is_also_rejected(self):
        result = self.execution(["ALPACA", "ALPACA"], ["IEX", "SIP"])
        self.assertEqual(result["status"], "MIXED_PROVIDER_DATA")

    def test_trigger_source_must_match_execution_source(self):
        result = self.execution(["YAHOO"], ["UNSPECIFIED"], dict(provider="ALPACA", feed="IEX", adjustment="raw"))
        self.assertEqual(result["status"], "MIXED_PROVIDER_DATA")

    def test_explicit_and_missing_provenance_is_rejected(self):
        self.assertEqual(source_identity([normal(), {}])["status"], "MIXED_PROVIDER_DATA")

    def test_legacy_input_remains_usable_without_forged_provider(self):
        self.assertEqual(source_identity([{}, {}])["status"], "LEGACY_UNSPECIFIED")

    def test_mixed_source_is_blocked_before_native_trigger_replay(self):
        from test_true_trades import document as native_document
        from true_trade_replay import replay
        doc = native_document()
        for i, row in enumerate(doc["bars"]["DEMO"]["5m"]):
            row.update(provider="ALPACA" if i < 1 else "YAHOO", feed="IEX", adjustment="raw")
        adapted = replay(MarketData(doc), CFG, "2026-11-09", "2026-11-09")
        self.assertFalse(any(p["ticker"] == "DEMO" for p in adapted["plans"]))
        self.assertTrue(any(a["reason"] == "MIXED_PROVIDER_DATA" for a in adapted["audit"]))

    def test_provider_changes_after_exit_cannot_reject_an_earlier_trade(self):
        from test_true_trades import row, plan
        first = dict(row(2, (105, 130, 104, 120)), provider="ALPACA", feed="IEX", adjustment="raw")
        later = dict(row(3), provider="YAHOO", feed="UNSPECIFIED", adjustment="split")
        result = reconstruct(plan(), [first, later], CLOCK, CFG, cost_scenarios(CFG)["IDEAL"])
        self.assertTrue(result["true_trade"])
        self.assertEqual(result["provider"], "ALPACA")

    def test_future_provider_changes_cannot_erase_an_earlier_trigger(self):
        from test_true_trades import document as native_document
        from true_trade_replay import replay
        doc = native_document()
        for i, row in enumerate(doc["bars"]["DEMO"]["5m"]):
            row.update(provider="ALPACA" if i < 2 else "YAHOO", feed="IEX", adjustment="raw")
        adapted = replay(MarketData(doc), CFG, "2026-11-09", "2026-11-09")
        self.assertTrue(any(p.get("provider") == "ALPACA" for p in adapted["plans"]))


class AcquisitionTests(unittest.TestCase):
    def test_completed_stock_session_is_not_downloaded_again(self):
        p = StubProvider("ALPACA")
        router = ProviderRouter([p], ["ALPACA"])
        with tempfile.TemporaryDirectory() as folder:
            one = collect(document(), {}, folder, "2026-11-09", "2026-11-09", router=router)
            two = collect(document(), {}, folder, "2026-11-09", "2026-11-09", router=router)
            self.assertEqual(one, two)
            self.assertEqual(len(p.calls), 2)
            self.assertEqual(two["bars"]["AAPL"]["1m"][0]["provider"], "ALPACA")

    def test_failed_stock_session_resumes_without_redownloading_completed_stock(self):
        class OneFailure(StubProvider):
            def fetch_bars(self, req):
                self.status = "PROVIDER_ERROR" if req.ticker == "MSFT" and not getattr(self, "recovered", False) else "AVAILABLE"
                return super().fetch_bars(req)
        p = OneFailure("ALPACA")
        router = ProviderRouter([p], ["ALPACA"])
        with tempfile.TemporaryDirectory() as folder:
            collect(document(("AAPL", "MSFT")), {}, folder, "2026-11-09", "2026-11-09", router=router)
            p.recovered = True
            result = collect(document(("AAPL", "MSFT")), {}, folder, "2026-11-09", "2026-11-09", router=router)
            self.assertEqual(sum(r.ticker == "AAPL" for r in p.calls), 2)
            self.assertTrue(result["bars"]["MSFT"]["1m"])
            self.assertEqual(len(read(Path(folder) / "progress.json")["errors"]), 1)

    def test_existing_intraday_is_replaced_as_a_whole(self):
        doc = document()
        old = execution_row(normal(provider="YAHOO", feed="UNSPECIFIED"))
        doc["bars"]["AAPL"]["1m"] = [old]
        p = StubProvider("ALPACA")
        with tempfile.TemporaryDirectory() as folder:
            result = collect(doc, {}, folder, "2026-11-09", "2026-11-09", router=ProviderRouter([p], ["ALPACA"]))
        self.assertEqual({r["provider"] for r in result["bars"]["AAPL"]["1m"]}, {"ALPACA"})

    def test_raw_bars_are_mapped_to_frozen_daily_split_basis(self):
        doc = document()
        # Split after the replay session: raw 100 becomes snapshot 50, then
        # existing session_rows restores raw 100. No double-adjustment.
        doc["splits"] = {"AAPL": {"future": dict(date=1794317400, numerator=2, denominator=1)}}
        certify_document(doc)
        p = StubProvider("ALPACA")
        with tempfile.TemporaryDirectory() as folder:
            result = collect(doc, {}, folder, "2026-11-09", "2026-11-09", router=ProviderRouter([p], ["ALPACA"]))
        self.assertEqual(result["bars"]["AAPL"]["1m"][0]["o"], 50)
        from true_trade_execution import session_rows
        _, rows = session_rows(MarketData(result), "AAPL", "2026-11-09")
        self.assertEqual(rows[0]["o"], 100)

    def test_mixed_primary_cannot_be_enabled_by_config(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "mixed provider"):
                collect(document(), {"mixed_provider_primary": True}, folder, "2026-11-09", "2026-11-09")


class AuditPilotTests(unittest.TestCase):
    def test_successful_free_protocol_uses_all_symbols_both_frames_and_older_day(self):
        probes = []
        def fake_probe(provider, req, kind):
            probes.append((req, kind))
            return dict(status="AVAILABLE", feed_verified=True, quality={"status": "VALID"},
                        kind=kind, pages=2, sessions=[kind])
        with tempfile.TemporaryDirectory() as folder, patch("provider_audit.probe", side_effect=fake_probe):
            result = alpaca_free_test(alpaca([]), "2026-11-12", folder)
        self.assertEqual(result["status"], "PASSED")
        regular = [r for r, kind in probes if kind == "FREE_IEX_REGULAR"]
        self.assertEqual(len(regular), 12)
        self.assertEqual({r.ticker for r in regular}, {"AAPL", "MSFT", "NVDA"})
        self.assertEqual({r.timeframe for r in regular}, {"1m", "5m"})
        self.assertEqual(len({r.start[:10] for r in regular}), 2)

    def test_last_completed_day_observes_holidays_and_close(self):
        self.assertEqual(last_completed_session("2026-11-13T10:00:00Z"), "2026-11-12")
        self.assertEqual(last_completed_session("2026-11-27T17:00:00Z"), "2026-11-25")
        self.assertEqual(last_completed_session("2026-11-27T18:20:00Z"), "2026-11-27")

    def test_no_auth_skips_free_and_historical_tests_without_network(self):
        with patch.dict("os.environ", {}, clear=True):
            p = AlpacaProvider(client=client("data.alpaca.markets", []))
        self.assertEqual(alpaca_free_test(p, "2026-11-12", "unused")["status"], "SKIPPED_MISSING_AUTH")
        self.assertEqual(historical_test(p, dt.datetime.now(dt.timezone.utc))["status"], "SKIPPED_MISSING_AUTH")
        self.assertEqual(p.client.fixture.calls, [])

    def test_free_test_refuses_sip(self):
        with self.assertRaisesRegex(ValueError, "IEX"):
            alpaca_free_test(alpaca([], feed="sip"), "2026-11-12", "unused")

    def test_auth_failure_stops_free_test_immediately(self):
        p = alpaca([(403, {}, {})])
        result = alpaca_free_test(p, "2026-11-12", "unused")
        self.assertEqual(result["stopped_on"], "AUTH_ERROR")
        self.assertEqual(len(p.client.fixture.calls), 1)

    def test_historical_test_records_windows_instead_of_asserting_year_wide_history(self):
        p = alpaca([ok(dict(bars=[]))] * 9)
        result = historical_test(p, "2026-11-13T10:00:00Z")
        self.assertEqual(set(result["years"]), {"2026", "2025", "2024", "2022", "2020", "2018", "2016"})
        self.assertTrue(all(r["status"] == "EMPTY" for r in result["years"].values()))
        self.assertEqual(len(result["old_tickers"]), 2)

    def test_pilot_is_blocked_before_successful_free_live_test(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "live test required"):
                pilot(document(), CFG, {}, folder, "2026-11-09", "2026-11-09")

    def test_paired_comparison_rejects_changed_daily_context(self):
        original, changed = document(), document()
        changed["evidence"]["events"] = [{"value": "different"}]
        with self.assertRaisesRegex(ValueError, "identical frozen"):
            comparison(original, changed, CFG, "2026-11-09", "2026-11-09")

    def test_paired_comparison_counts_identical_setup_ids(self):
        from test_true_trades import document as native_document
        original, changed = native_document(), native_document()
        for row in changed["bars"]["DEMO"]["5m"]:
            row.update(provider="ALPACA", feed="IEX", adjustment="split")
        report = comparison(original, changed, CFG, "2026-11-09", "2026-11-09")
        self.assertTrue(report["same_setup_event_ids"])
        self.assertEqual(report["counts"]["YAHOO"]["BREAKOUT"]["setup_ready"], 1)
        self.assertEqual(report["counts"]["YAHOO"], report["counts"]["ALPACA"])
        self.assertNotIn("profitability", report)


if __name__ == "__main__":
    unittest.main()
