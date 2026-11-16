"""Four review regressions; synthetic responses only, no network access."""
import copy
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_intraday_providers import (AUTH, CFG, CLOCK, END, START, StubProvider, alpaca,
    alpaca_bar, client, document, massive, massive_bar, normal, ok, request)
from common import digest, iso, stamp
from data import MarketData
from engine import cost_scenarios
from intraday_providers import AlpacaProvider, BarCache, BarRequest, MassiveProvider, ProviderRouter, YahooProvider, execution_row
from provider_collect import collect
from true_trade_execution import reconstruct, session_rows
from true_trade_replay import replay


def certificate(bars, splits, anchor="2026-11-10", since="2024-01-01"):
    return dict(adjustment="split", anchor_date=anchor, complete_from=since, complete_through=anchor,
                bars_sha256=digest(bars), splits_sha256=digest(splits), evidence_id="offline-archived-snapshot")


def frozen_document():
    doc = document()
    doc["provenance"]["price_basis"] = "YAHOO_SPLIT_ADJUSTED"
    doc["provenance"]["daily_price_basis"] = {"AAPL": certificate(doc["bars"]["AAPL"]["1d"], {})}
    return doc


class SourceWithBasis(StubProvider):
    def __init__(self, price=100, source_splits=None, anchor="2026-11-10", certified=True):
        super().__init__("YAHOO")
        self.price, self.source_splits, self.anchor, self.certified = price, source_splits or {}, anchor, certified

    def fetch_bars(self, req):
        result = super().fetch_bars(req)
        for bar in result["bars"]:
            bar.update(open=self.price, high=self.price + 1, low=self.price - 1, close=self.price,
                       vwap=self.price, volume=2000 if self.price == 50 else 1000)
        if self.certified:
            result["basis_evidence"] = dict(certificate(result["bars"], self.source_splits, self.anchor), splits=self.source_splits)
        return result


def collect_one(doc, provider, config=None):
    with tempfile.TemporaryDirectory() as folder:
        return collect(doc, config or {}, folder, "2026-11-09", "2026-11-09",
                       router=ProviderRouter([provider], [provider.name]))


class ReviewOneCausalSelection(unittest.TestCase):
    def test_structural_error_quarantines_its_suffix_without_sorting_or_interpolation(self):
        rows = [alpaca_bar(), alpaca_bar("2026-11-09T14:31:00Z"), alpaca_bar("2026-11-09T14:31:00Z"),
                alpaca_bar("2026-11-09T14:32:00Z")]
        result = alpaca([ok(dict(bars=rows))]).fetch_bars(request())
        self.assertEqual([b["timestamp_utc"] for b in result["bars"]], [START, "2026-11-09T14:31:00Z"])
        self.assertEqual([b["index"] for b in result["quarantined_bars"]], [2, 3])
        self.assertEqual(result["validation_cutoff"]["index"], 2)

    def test_later_pagination_failure_retains_same_provider_validated_prefix(self):
        a = alpaca([ok(dict(bars=[alpaca_bar()], next_page_token="next")), (503, {}, {})])
        a.client.retries = 0
        y = StubProvider("YAHOO")
        selected = ProviderRouter([a, y], ["ALPACA", "YAHOO"]).fetch_trade([request()], opening=START)
        self.assertEqual(selected["provider"], "ALPACA")
        self.assertEqual(selected["results"][0]["status"], "AVAILABLE_PREFIX")
        self.assertEqual(selected["results"][0]["download_status"], "PROVIDER_ERROR")
        self.assertFalse(selected["results"][0]["download_complete"])
        self.assertEqual(y.calls, [])

    def test_invalid_opening_allows_fallback_but_never_uses_later_openings(self):
        bad = dict(alpaca_bar(), v=-1)
        a, y = alpaca([ok(dict(bars=[bad, alpaca_bar("2026-11-09T14:31:00Z")]))]), StubProvider("YAHOO")
        selected = ProviderRouter([a, y], ["ALPACA", "YAHOO"]).fetch_trade([request()], opening=START)
        self.assertEqual(selected["provider"], "YAHOO")

    def test_resume_does_not_replace_a_pinned_provider_when_auth_disappears(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = BarCache(folder)
            a = alpaca([ok(dict(bars=[alpaca_bar()], next_page_token="next")), (503, {}, {}),
                        (503, {}, {})], cache=cache)
            a.client.retries = 0
            y = StubProvider("YAHOO")
            router = ProviderRouter([a, y], ["ALPACA", "YAHOO"])
            first = collect(frozen_document(), {}, folder + "/run", "2026-11-09", "2026-11-09", router=router)
            self.assertTrue(first["bars"]["AAPL"]["1m"])
            with patch.dict("os.environ", {}, clear=True):
                resumed = AlpacaProvider(client=client("data.alpaca.markets", []), cache=cache)
            second = collect(frozen_document(), {}, folder + "/run", "2026-11-09", "2026-11-09",
                router=ProviderRouter([resumed, y], ["ALPACA", "YAHOO"]))
            self.assertEqual(second["bars"]["AAPL"]["1m"], [])
            self.assertEqual(second["provenance"]["provider_records"][0]["status"], "DATA_UNAVAILABLE")
            self.assertEqual(y.calls, [])

    def selected(self, duplicate=False):
        rows = [dict(alpaca_bar(), o=106.1, h=106.3, l=106, c=106.2, v=600000),
                dict(alpaca_bar("2026-11-09T14:35:00Z"), o=106.2, h=107, l=106.1, c=106.8, v=600000),
                dict(alpaca_bar("2026-11-09T14:40:00Z"), o=106.8, h=110, l=106.5, c=109, v=600000),
                alpaca_bar("2026-11-09T14:50:00Z")]
        if duplicate:
            rows += [dict(rows[-1])]
        a, y = alpaca([ok(dict(bars=rows))]), StubProvider("YAHOO")
        return ProviderRouter([a, y], ["ALPACA", "YAHOO"]).fetch_trade([request("5m")], opening=START), y

    def test_r1_later_duplicate_preserves_opening_provider(self):
        good, _ = self.selected(False)
        damaged, fallback = self.selected(True)
        self.assertEqual(good["provider"], "ALPACA")
        self.assertEqual(damaged["provider"], good["provider"])
        self.assertEqual(fallback.calls, [])
        self.assertEqual(damaged["results"][0]["quality"]["status"], "INVALID")
        self.assertEqual(len({b["timestamp_utc"] for b in damaged["results"][0]["bars"]}),
                         len(damaged["results"][0]["bars"]))

    def test_r1_later_duplicate_preserves_native_trigger_and_completed_trade(self):
        from test_true_trades import document as native_document
        records = []
        for duplicate in (False, True):
            selection, _ = self.selected(duplicate)
            doc = native_document()
            doc["bars"]["DEMO"]["5m"] = [execution_row(b) for b in selection["results"][0]["bars"]]
            market = MarketData(doc)
            plans = replay(market, CFG, "2026-11-09", "2026-11-09")["plans"]
            self.assertTrue(any(p["strategy"] == "BREAKOUT" for p in plans), "late data error erased the earlier native trigger")
            plan = next(p for p in plans if p["strategy"] == "BREAKOUT")
            _, rows = session_rows(market, "DEMO", "2026-11-09")
            trade = reconstruct(plan, rows, CLOCK, CFG, cost_scenarios(CFG)["REALISTIC"])
            self.assertTrue(trade["true_trade"])
            records.append((plan, trade))
        self.assertEqual(records[0], records[1])


class ReviewTwoYahooFrames(unittest.TestCase):
    def test_collector_uses_proven_yahoo_5m_when_1m_history_is_unavailable(self):
        yahoo = SourceWithBasis()
        yahoo.by_frame = {"1m": "INTRADAY_HISTORY_LIMITED"}
        result = collect_one(frozen_document(), yahoo)
        self.assertEqual(result["bars"]["AAPL"]["1m"], [])
        self.assertTrue(result["bars"]["AAPL"]["5m"])
        self.assertEqual(session_rows(MarketData(result), "AAPL", "2026-11-09")[0], "5m")
        self.assertEqual(result["provenance"]["provider_records"][0]["status"], "AVAILABLE")

    def test_r2_history_limited_1m_still_requests_yahoo_5m(self):
        calls = []
        def fetch(ticker, params, path):
            calls.append(params["interval"])
            quotes = dict(open=[100], high=[102], low=[99], close=[101], volume=[1000])
            import json
            return {"http_status": 200}, json.dumps(dict(chart=dict(error=None, result=[dict(
                meta=dict(symbol=ticker, dataGranularity=params["interval"]),
                timestamp=[1794234600], indicators=dict(quote=[quotes]))]))).encode()
        yahoo = YahooProvider(requester=fetch, now=lambda: "2026-11-20T10:00:00Z")
        selection = ProviderRouter([yahoo], ["YAHOO"]).fetch_trade([request(), request("5m")], opening=START)
        self.assertEqual(selection["provider"], "YAHOO")
        self.assertEqual(calls, ["5m"])
        self.assertEqual([r["status"] for r in selection["results"]], ["INTRADAY_HISTORY_LIMITED", "AVAILABLE"])
        doc = document()
        for result in selection["results"]:
            doc["bars"]["AAPL"][result["request"]["timeframe"]] = [execution_row(b) for b in result["bars"]]
        frame, _ = session_rows(MarketData(doc), "AAPL", "2026-11-09")
        self.assertEqual(frame, "5m")


class ReviewThreeCachedDownloads(unittest.TestCase):
    def test_complete_massive_cache_is_usable_without_auth(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = BarCache(folder)
            first = massive([ok(dict(status="OK", adjusted=False, results=[massive_bar()]))], cache=cache).fetch_bars(request())
            with patch.dict("os.environ", {}, clear=True):
                p = MassiveProvider(client=client("api.massive.com", []), cache=cache)
            self.assertEqual(p.fetch_bars(request())["bars"], first["bars"])
            self.assertEqual(p.client.fixture.calls, [])

    def test_complete_cache_still_checks_integrity_without_auth(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = BarCache(folder)
            alpaca([ok(dict(bars=[alpaca_bar()]))], cache=cache).fetch_bars(request())
            next(Path(folder).rglob("page-*.json")).write_text("[]")
            with patch.dict("os.environ", {}, clear=True):
                p = AlpacaProvider(client=client("data.alpaca.markets", []), cache=cache)
            with self.assertRaisesRegex(ValueError, "checksum"):
                p.fetch_bars(request())
            self.assertEqual(p.client.fixture.calls, [])

    def test_r3_complete_yahoo_cache_survives_retention_expiry(self):
        from test_intraday_providers import YahooTests
        with tempfile.TemporaryDirectory() as folder:
            p = YahooTests().provider()
            p.cache = BarCache(folder)
            original = p.fetch_bars(request(adjustment="split"))
            p.now = lambda: "2027-11-13T10:00:00Z"
            with patch.object(p, "requester", side_effect=AssertionError("cache must not download")):
                cached = p.fetch_bars(request(adjustment="split"))
            self.assertEqual(cached["status"], "AVAILABLE")
            self.assertTrue(cached["cache_hit"])
            self.assertEqual(cached["bars"], original["bars"])
            self.assertEqual(cached["http_events"], [])

    def test_r3_complete_cache_is_usable_without_auth(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = BarCache(folder)
            original = alpaca([ok(dict(bars=[alpaca_bar()]))], cache=cache).fetch_bars(request())
            with patch.dict("os.environ", {}, clear=True):
                p = AlpacaProvider(client=client("data.alpaca.markets", []), cache=cache)
            cached = p.fetch_bars(request())
            self.assertEqual(cached["status"], "AVAILABLE")
            self.assertTrue(cached["cache_hit"])
            self.assertEqual(cached["bars"], original["bars"])
            self.assertEqual(p.client.fixture.calls, [])

    def test_r3_incomplete_checkpoint_still_requires_auth(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = BarCache(folder)
            key = request().key("ALPACA", "IEX")
            state, _ = cache.load(key)
            cache.page(key, state, [normal()], "next")
            with patch.dict("os.environ", {}, clear=True):
                p = AlpacaProvider(client=client("data.alpaca.markets", []), cache=cache)
            result = p.fetch_bars(request())
            self.assertEqual(result["status"], "DATA_UNAVAILABLE")
            self.assertEqual(result["bars"], [])
            self.assertFalse(cache.load(key)[0]["complete"])
            self.assertEqual(p.client.fixture.calls, [])

    def test_r3_unfinalized_last_page_is_not_a_complete_auth_bypass(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = BarCache(folder)
            key = request().key("ALPACA", "IEX")
            state, _ = cache.load(key)
            cache.page(key, state, [normal()], None)
            with patch.dict("os.environ", {}, clear=True):
                p = AlpacaProvider(client=client("data.alpaca.markets", []), cache=cache)
            self.assertEqual(p.fetch_bars(request())["status"], "DATA_UNAVAILABLE")
            self.assertFalse(cache.load(key)[0]["complete"])

    def test_r3_incomplete_yahoo_checkpoint_still_obeys_retention(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = BarCache(folder)
            req = request(adjustment="split")
            key = req.key("YAHOO", "UNSPECIFIED")
            state, _ = cache.load(key)
            cache.page(key, state, [normal(provider="YAHOO", feed="UNSPECIFIED", req=req)], "next")
            p = YahooProvider(cache=cache, now=lambda: "2027-11-13T10:00:00Z",
                              requester=lambda *a: self.fail("expired checkpoint must not download"))
            result = p.fetch_bars(req)
            self.assertEqual(result["status"], "INTRADAY_HISTORY_LIMITED")
            self.assertEqual(result["bars"], [])
            self.assertFalse(cache.load(key)[0]["complete"])


class ReviewFourPriceBasis(unittest.TestCase):
    def test_older_adjusted_source_can_be_mapped_to_a_newer_frozen_basis(self):
        doc = frozen_document()
        split = {"later": dict(date=1794493800, numerator=2, denominator=1)}
        doc["splits"] = {"AAPL": split}
        doc["provenance"]["daily_price_basis"]["AAPL"] = certificate([], split, "2026-11-13")
        result = collect_one(doc, SourceWithBasis(100, {}, "2026-11-10"))
        row = result["bars"]["AAPL"]["1m"][0]
        self.assertEqual((row["o"], row["v"], row["basis_price_factor"]), (50, 2000, .5))
        _, native = session_rows(MarketData(result), "AAPL", "2026-11-09")
        self.assertEqual(native[0]["o"], 100)

    def test_source_snapshot_hash_must_match_the_actual_bars(self):
        class Stale(SourceWithBasis):
            def fetch_bars(self, req):
                result = super().fetch_bars(req)
                result["basis_evidence"]["bars_sha256"] = "0" * 64
                return result
        result = collect_one(frozen_document(), Stale())
        self.assertEqual(result["provenance"]["provider_records"][0]["status"], "BASIS_EVIDENCE_MISMATCH")
        self.assertEqual(result["bars"]["AAPL"]["1m"], [])

    def test_declared_anchor_without_complete_ledger_is_unknown(self):
        class Incomplete(SourceWithBasis):
            def fetch_bars(self, req):
                result = super().fetch_bars(req)
                result["basis_evidence"]["complete_from"] = "2026-11-10"
                return result
        result = collect_one(frozen_document(), Incomplete())
        self.assertEqual(result["provenance"]["provider_records"][0]["status"], "BASIS_COMPATIBILITY_UNKNOWN")

    def test_unknown_basis_rows_cannot_generate_native_plans(self):
        from test_true_trades import document as native_document
        doc = native_document()
        for row in doc["bars"]["DEMO"]["5m"]:
            row.update(provider="YAHOO", feed="UNSPECIFIED", adjustment="split", storage_basis="FROZEN_DAILY_SPLIT_BASIS")
        adapted = replay(MarketData(doc), CFG, "2026-11-09", "2026-11-09")
        self.assertEqual(adapted["plans"], [])
        self.assertTrue(any(a["reason"] == "BASIS_COMPATIBILITY_UNKNOWN" for a in adapted["audit"]))

    def test_unverified_basis_cannot_produce_a_primary_trade(self):
        from test_true_trades import row, plan
        r = dict(row(2, (105, 130, 104, 120)), provider="ALPACA", feed="IEX", adjustment="raw",
                 storage_basis="FROZEN_DAILY_SPLIT_BASIS", basis_status="UNVERIFIED")
        trade = reconstruct(plan(), [r], CLOCK, CFG, cost_scenarios(CFG)["REALISTIC"])
        self.assertFalse(trade["true_trade"])
        self.assertEqual(trade["status"], "BASIS_COMPATIBILITY_UNKNOWN")

    def test_future_basis_mismatch_after_exit_does_not_erase_a_completed_trade(self):
        from test_true_trades import row, plan
        first = dict(row(2, (105, 130, 104, 120)), provider="ALPACA", feed="IEX", adjustment="raw",
                     storage_basis="FROZEN_DAILY_SPLIT_BASIS", storage_basis_id="daily-1", basis_status="VERIFIED")
        later = dict(row(3), provider="ALPACA", feed="IEX", adjustment="raw",
                     storage_basis="FROZEN_DAILY_SPLIT_BASIS", storage_basis_id="daily-2", basis_status="VERIFIED")
        trade = reconstruct(plan(), [first, later], CLOCK, CFG, cost_scenarios(CFG)["REALISTIC"])
        self.assertTrue(trade["true_trade"])

    def test_r4_unknown_split_basis_is_an_explicit_exclusion(self):
        result = collect_one(frozen_document(), SourceWithBasis(certified=False))
        self.assertEqual(result["bars"]["AAPL"]["1m"], [])
        self.assertEqual(result["provenance"]["provider_records"][0]["status"], "BASIS_COMPATIBILITY_UNKNOWN")

    def test_r4_same_proven_basis_is_accepted(self):
        result = collect_one(frozen_document(), SourceWithBasis())
        row = result["bars"]["AAPL"]["1m"][0]
        self.assertEqual(row["o"], 100)
        self.assertEqual(row["adjustment"], "split")
        self.assertEqual(row["storage_basis"], "FROZEN_DAILY_SPLIT_BASIS")
        self.assertEqual(row.get("basis_status"), "VERIFIED")

    def test_r4_split_between_daily_snapshot_and_intraday_fetch_is_rebased(self):
        # Daily snapshot 10 Nov: pre-split historical price 100. A 2:1 split
        # on 12 Nov makes the later adjusted intraday quote for 9 Nov equal 50.
        split = {"later": dict(date=1794493800, numerator=2, denominator=1)}
        result = collect_one(frozen_document(), SourceWithBasis(50, split, "2026-11-13"))
        row = result["bars"]["AAPL"]["1m"][0]
        self.assertEqual(row["o"], 100)
        self.assertEqual(row["v"], 1000)
        self.assertEqual(row["vwap"], 100)
        self.assertEqual(row["adjustment"], "split")
        self.assertEqual(row["basis_price_factor"], 2)
        self.assertEqual(row.get("basis_status"), "VERIFIED")

    def test_r4_unknown_daily_ledger_does_not_label_raw_as_verified(self):
        doc = frozen_document()
        del doc["provenance"]["daily_price_basis"]
        result = collect_one(doc, StubProvider("ALPACA"))
        self.assertEqual(result["bars"]["AAPL"]["1m"], [])
        self.assertEqual(result["provenance"]["provider_records"][0]["status"], "BASIS_COMPATIBILITY_UNKNOWN")

    def test_r4_raw_source_is_independent_of_a_later_source_split(self):
        result = collect_one(frozen_document(), StubProvider("ALPACA"))
        row = result["bars"]["AAPL"]["1m"][0]
        self.assertEqual(row["o"], 100)
        self.assertEqual(row["adjustment"], "raw")
        self.assertEqual(row.get("basis_status"), "VERIFIED")

    def test_r4_conflicting_split_ledgers_are_excluded(self):
        doc = frozen_document()
        doc["splits"] = {"AAPL": {"action": dict(date=1794317400, numerator=2, denominator=1)}}
        doc["provenance"]["daily_price_basis"]["AAPL"] = certificate([], doc["splits"]["AAPL"], "2026-11-13")
        conflicting = {"action": dict(date=1794317400, numerator=3, denominator=1)}
        result = collect_one(doc, SourceWithBasis(50, conflicting, "2026-11-13"))
        self.assertEqual(result["bars"]["AAPL"]["1m"], [])
        self.assertEqual(result["provenance"]["provider_records"][0]["status"], "BASIS_INCOMPATIBLE")


if __name__ == "__main__":
    unittest.main()
