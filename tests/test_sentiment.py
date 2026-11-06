"""Offline screenshot-transcription tests; no OCR or live data is claimed.

The supported long path reuses the actual TSLA entry fixture and the real
scanner, news parser, entry, exit and risk modules. Sentiment/quotes/account
metadata are synthetic. Neither tests nor the new helper import a broker.
"""

import copy
import datetime
import io
import json
import os
import socket
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from _load import FIX, ROOT, load

sentiment = load("trader-sentiment", "sentiment")
scanner = load("trader-scanner", "scan")
entry = load("trader-entry", "entry")
exitp = load("trader-exit", "exit")
news = load("trader-news", "news")
NOW = "2026-10-19T14:15:00Z"
EXAMPLE = os.path.join(ROOT, "trader-sentiment", "screenshot.example.json")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = sentiment.main(argv)
    return code, out.getvalue(), err.getvalue()


def base_document():
    """Synthetic stock paper study, not a relabelled Plus500 CFD."""
    with open(os.path.join(FIX, "trader-entry", "TSLA-2026-10-19-5m.json")) as handle:
        raw = handle.read()
    utc = datetime.datetime(2026, 10, 19, 14, 15)
    series = scanner.parse_chart(raw, "TSLA")
    cutoff = datetime.datetime(2026, 10, 19, 10, 15)
    hits, _, _ = scanner.scan_orb({"TSLA": series}, ["TSLA"], utc.date(), cutoff, 15)
    checked_entry = entry.public(entry.evaluate(entry.parse_chart(raw, "TSLA"), utc))
    bars = scanner.session_bars(series, utc.date(), cutoff)
    last = [{"close_time": (scanner.eastern_to_utc(bar["start"]) + datetime.timedelta(minutes=5)).isoformat() + "Z",
             "close": bar["c"]} for bar in bars[-3:]]
    output = io.StringIO()
    with redirect_stdout(output):
        status = exitp.main(["plan", "--symbol", "TSLA", "--entry", "364.18", "--or-high", "362.18", "--or-low", "354.47",
                             "--now", "2026-10-19 10:15", "--target", "2R", "--json"])
    assert status == 0
    with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as handle:
        config = json.load(handle)
    with open(os.path.join(FIX, "trader-news", "yahoo-TSLA.xml")) as handle:
        items = news._json_ready(news.parse_rss(handle.read(), "TSLA"))
    return {
        "source": "Plus500 +Insights", "synthetic": True,
        "screenshot": {"file": "SYNTHETIC stock paper study, no screenshot", "captured_at": NOW,
                       "locale": "de-DE", "category": "highest_buy", "sentiment_period": "Letzte 24 Stunden"},
        "candidates": [{"instrument": "Tesla stock (synthetic paper study)", "symbol": "TSLA", "instrument_type": "stock", "currency": "USD",
                        "buy_pct": "90 %", "sell_pct": "10 %", "daily_change_pct": "+2,67 %", "buy_price": "364,20",
                        "sell_price": "364,00", "spread": None, "leverage": "1:1",
                        "context": {
                            "market": {"symbol": "TSLA", "instrument_type": "stock", "currency": "USD", "mapping_verified": True,
                                       "same_price_basis": True, "source": "Yahoo TSLA offline fixture; synthetic period change",
                                       "as_of": NOW, "interval": "5m", "bars": last, "sentiment_period": "Letzte 24 Stunden",
                                       "period_verified": True, "period_change_pct": 2.67},
                            "scanner": {"created_by": "trader-scanner", "created_at": NOW, "mode": "orb",
                                        "as_of": "2026-10-19 10:15 ET", "hits": hits},
                            "news": {"checked": NOW, "items": items, "notes": []},
                            "news_review": {"symbol": "TSLA", "reviewed": True, "blocking_catalyst": False,
                                            "reason": "Synthetic offline review only; not a current news assessment"},
                            "entry": checked_entry, "exit": json.loads(output.getvalue()),
                            "risk_inputs": {"as_of": NOW, "config": config, "qty": 8,
                                            "portfolio": {"cash": 5000, "positions": []}, "state": {"orders": []}}
                        }}]
    }


class Extraction(unittest.TestCase):
    def test_locales_and_currency(self):
        for value, locale, expected in [("1.234,56", "de-DE", 1234.56), ("1,234.56", "en-US", 1234.56),
                                         ("-1,25 %", "de-DE", -1.25), ("USD 364,20", "de-DE", 364.2),
                                         ("\u22120,25%", "de-DE", -0.25), (0, "de-DE", 0)]:
            with self.subTest(value=value):
                self.assertEqual(sentiment.number(value, locale), expected)

    def test_missing_is_never_zero_or_inferred(self):
        for value in (None, "", "?", "unleserlich", "-"):
            self.assertIsNone(sentiment.number(value))
        with open(EXAMPLE) as handle:
            doc = json.load(handle)
        doc["candidates"][0]["sell_pct"] = None
        row = sentiment.analyze(doc, "2026-11-06T15:15:00Z", extract_only=True)["candidates"][0]
        self.assertIsNone(row["sell_pct"])
        self.assertIn("sell_pct", row["missing_fields"])
        self.assertEqual(row["raw"]["buy_pct"], "91 %")

    def test_ambiguous_nonfinite_and_boolean_values_are_rejected(self):
        for value in ("1,234.56", "NaN", float("nan"), float("inf"), True, {}, "90O", "1,2,3"):
            with self.subTest(value=value), self.assertRaises(sentiment.SentimentError):
                sentiment.number(value, "de-DE")

    def test_leverage_ratios(self):
        for text in ("1:5", "5:1", "5x", "x5", "5"):
            self.assertEqual(sentiment.number(text, leverage=True), 5)
        for text in ("0:5", "2:5"):
            with self.assertRaises(sentiment.SentimentError):
                sentiment.number(text, leverage=True)

    def test_spread_is_labelled_as_derived(self):
        with open(EXAMPLE) as handle:
            doc = json.load(handle)
        row = sentiment.normalize(doc["candidates"][0], doc["screenshot"])
        self.assertEqual(row["spread_derived"], 0.2)
        self.assertIsNone(row["spread_reported"])
        self.assertEqual(row["spread"], 0.2)


class SentimentInterpretation(unittest.TestCase):
    def test_agreement_and_both_contrarian_cases(self):
        cases = [(90, 10, 1, [1, 2, 3], "LONG", False),
                 (10, 90, -1, [3, 2, 1], "SHORT", False),
                 (90, 10, -1, [3, 2, 1], "SHORT", True),
                 (10, 90, 1, [1, 2, 3], "LONG", True)]
        for buy, sell, change, closes, side, contrarian in cases:
            with self.subTest(buy=buy, change=change):
                bias, why = sentiment.sentiment_bias(buy, sell, change, closes)
                self.assertEqual(bias, side)
                self.assertEqual("kontraer" in why, contrarian)

    def test_extreme_majority_alone_or_one_day_change_never_confirms(self):
        cases = [(99, 1, None, []), (1, 99, 2, []), (90, 10, 1, [1, 2, 1]),
                 (50, 50, 1, [1, 2, 3]), (80, 20, -1, [3, 2, 1]), (90, 10, -1, [1, 2, 3])]
        for args in cases:
            with self.subTest(args=args):
                self.assertIsNone(sentiment.sentiment_bias(*args)[0])


class EvidenceGates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = base_document()

    def setUp(self):
        self.doc = copy.deepcopy(self.template)
        self.row = self.doc["candidates"][0]
        self.ctx = self.row["context"]

    def result(self, now=NOW):
        return sentiment.analyze(self.doc, now)["candidates"][0]

    def blocked(self, expected):
        result = self.result()
        self.assertEqual(result["decision"], "KEIN TRADE", result)
        self.assertIsNone(result["stop"])
        self.assertIsNone(result["target"])
        self.assertIn(expected, " | ".join(result["reasons"]))

    def test_complete_long_calls_real_risk_without_network_or_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            try:
                os.chdir(directory)
                with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
                    result = self.result()
                self.assertEqual(os.listdir(directory), [])
            finally:
                os.chdir(previous)
        self.assertEqual(result["decision"], "LONG", result)
        self.assertEqual(result["risk"]["results"][0]["verdict"], "PASS")
        self.assertEqual(result["stop"], 358.32)
        self.assertEqual(result["target"], 375.9)
        self.assertTrue(result["risk"]["paper"])

    def test_confirmed_contrarian_long_is_only_medium_quality(self):
        self.row.update(buy_pct=10, sell_pct=90, category="highest_sell")
        result = self.result()
        self.assertEqual(result["decision"], "LONG", result)
        self.assertIn("mittel", result["quality"])

    def test_recent_entry_check_does_not_require_execution_in_the_same_second(self):
        result = self.result("2026-10-19T14:18:00Z")
        self.assertEqual(result["decision"], "LONG", result)

    def test_unverified_calendar_year_cannot_be_approved(self):
        result = self.result("2028-10-19T14:15:00Z")
        self.assertEqual(result["decision"], "KEIN TRADE")
        self.assertIn("Handelskalender", " | ".join(result["reasons"]))

    def test_missing_each_required_screenshot_field_blocks(self):
        for field in ("buy_pct", "sell_pct", "daily_change_pct", "buy_price", "sell_price", "leverage", "symbol", "currency"):
            with self.subTest(field=field):
                saved = self.row[field]
                self.row[field] = None
                self.assertEqual(self.result()["decision"], "KEIN TRADE")
                self.row[field] = saved

    def test_sentiment_alone_does_not_confirm_trade(self):
        self.row.pop("context")
        self.blocked("Kursdaten")

    def test_inconsistent_percentages_and_reversed_quotes(self):
        self.row["sell_pct"] = 90
        self.blocked("widerspruechlich")
        self.row["sell_pct"] = 10
        self.row["sell_price"] = 365
        self.blocked("Spalten pruefen")

    def test_wide_spread_and_cost_relative_to_stop(self):
        self.row["sell_price"] = 360
        self.blocked("0,5 Prozent")
        self.row["sell_price"] = 362.7
        self.blocked("20 Prozent")

    def test_reported_spread_must_have_a_known_unit_and_match_quotes(self):
        self.row.update(spread="0,20", spread_unit="pips")
        self.blocked("Einheit unklar")
        self.row.update(spread="0,10", spread_unit="price")
        self.blocked("passt nicht")

    def test_missing_timezone_stale_and_future_capture(self):
        for stamp in (None, "2026-10-19T10:15:00", "2026-10-19T14:09:59Z", "2026-10-19T14:16:00Z"):
            with self.subTest(stamp=stamp):
                self.doc["screenshot"]["captured_at"] = stamp
                self.blocked("Screenshot")

    def test_stale_evidence_from_each_skill_blocks(self):
        for key, field in (("scanner", "created_at"), ("news", "checked"), ("exit", "created_at"), ("risk_inputs", "as_of"), ("market", "as_of")):
            with self.subTest(key=key):
                saved = self.ctx[key][field]
                self.ctx[key][field] = "2026-10-19T12:00:00Z"
                self.assertEqual(self.result()["decision"], "KEIN TRADE")
                self.ctx[key][field] = saved

    def test_daily_change_is_not_a_substitute_for_matching_period(self):
        self.ctx["market"]["sentiment_period"] = "Heute"
        self.blocked("vergleichbaren Zeitraum")

    def test_unverified_period_mapping_and_price_basis_block(self):
        for field, expected in (("mapping_verified", "Zuordnung"), ("same_price_basis", "Preisbasis"), ("period_verified", "Zeitraum")):
            with self.subTest(field=field):
                self.ctx["market"][field] = False
                self.assertEqual(self.result()["decision"], "KEIN TRADE")
                self.ctx["market"][field] = True

    def test_forming_missing_and_stale_bars_block(self):
        original = copy.deepcopy(self.ctx["market"]["bars"])
        for bars in (original[:1], original[::2], original + [{"close_time": "2026-10-19T14:20:00Z", "close": 365}]):
            with self.subTest(bars=bars):
                self.ctx["market"]["bars"] = bars
                self.blocked("Kursbalken")

    def test_news_missing_blocking_or_partial_is_not_a_clean_review(self):
        self.ctx["news"]["notes"] = ["SEC feed unavailable"]
        self.blocked("Nachrichten")
        self.ctx["news"]["notes"] = []
        self.ctx["news_review"]["blocking_catalyst"] = True
        self.blocked("Nachrichten")

    def test_other_symbols_or_oversold_alone_are_not_a_scanner_setup(self):
        self.ctx["scanner"]["hits"][0]["symbol"] = "NVDA"
        self.blocked("Kursstruktur")
        self.ctx["scanner"]["hits"][0].update(symbol="TSLA", setups=["oversold"])
        self.blocked("oversold allein")

    def test_stale_daily_chart_is_not_hidden_by_a_fresh_fetch_time(self):
        self.ctx["scanner"].update(mode="daily", hits=[{"symbol": "TSLA", "setups": ["breakout_20d"], "close_date": "2026-10-15"}])
        self.blocked("juengsten abgeschlossenen Handelstag")

    def test_waiting_or_expired_entry_cannot_be_promoted(self):
        for status in ("waiting", "expired", "no-data"):
            with self.subTest(status=status):
                self.ctx["entry"]["status"] = status
                self.blocked("Long-Trigger")

    def test_stale_trigger_is_not_hidden_by_a_fresh_check(self):
        self.ctx["entry"]["trigger"]["close_time"] = "09:50"
        self.blocked("Entry-Triggerzeit")

    def test_unmatched_exit_and_invalid_stop_target_block(self):
        for field, value in (("symbol", "NVDA"), ("entry", 365), ("stop", 400), ("target", 300), ("side", "sell")):
            with self.subTest(field=field):
                original = self.ctx["exit"][field]
                self.ctx["exit"][field] = value
                self.blocked("trader-exit-Plan")
                self.ctx["exit"][field] = original

    def test_cfd_leverage_and_currency_are_not_stock_risk(self):
        for field, value in (("instrument_type", "cfd"), ("leverage", 5), ("currency", "EUR")):
            with self.subTest(field=field):
                saved = self.row[field]
                self.row[field] = value
                self.blocked("nicht unterstuetzt")
                self.row[field] = saved

    def test_short_bias_remains_no_trade_with_long_only_entry_exit(self):
        self.ctx["market"]["period_change_pct"] = -2
        for bar, close in zip(self.ctx["market"]["bars"], [366, 365, 364]):
            bar["close"] = close
        result = self.result()
        self.assertEqual(result["bias"], "SHORT")
        self.blocked("nur Long")

    def test_real_risk_limits_cannot_be_replaced_with_claimed_pass(self):
        self.ctx["risk"] = {"verdict": "PASS"}
        self.ctx["risk_inputs"]["config"]["max_loss_per_trade_usd"] = 1
        result = self.result()
        self.assertEqual(result["risk"]["results"][0]["verdict"], "BLOCK")
        self.blocked("trader-risk")

    def test_daily_limits_live_config_and_missing_account_block(self):
        self.ctx["risk_inputs"]["state"]["orders"] = [{"symbol": "NVDA", "side": "buy", "status": "placed", "date": "2026-10-19", "kind": "entry", "max_loss": 150}]
        self.blocked("trader-risk")
        self.ctx["risk_inputs"]["config"]["paper"] = False
        self.blocked("Paper-Konfiguration")
        self.ctx["risk_inputs"].pop("portfolio")
        self.blocked("Kontostand")

    def test_market_closed_weekend_and_holiday_block_even_with_fresh_snapshot(self):
        for now in ("2026-10-19T12:15:00Z", "2026-10-24T14:15:00Z", "2026-11-26T15:15:00Z"):
            with self.subTest(now=now):
                result = self.result(now)
                self.assertEqual(result["decision"], "KEIN TRADE")
                self.assertIn("US-Aktienhandelszeit", " | ".join(result["reasons"]))

    def test_bad_candidate_does_not_abort_others(self):
        self.doc["candidates"].insert(0, "not an object")
        result = sentiment.analyze(self.doc, NOW)["candidates"]
        self.assertEqual(result[0]["decision"], "KEIN TRADE")
        self.assertEqual(result[1]["decision"], "LONG", result[1])


class CommandLine(unittest.TestCase):
    def test_synthetic_example_is_no_trade_with_both_biases(self):
        code, out, err = run([EXAMPLE, "--now", "2026-11-06T15:15:00Z", "--json"])
        self.assertEqual((code, err), (0, ""))
        report = json.loads(out)
        self.assertEqual([r["decision"] for r in report["candidates"]], ["KEIN TRADE", "KEIN TRADE"])
        self.assertEqual([r["bias"] for r in report["candidates"]], ["SHORT", "LONG"])
        self.assertEqual(report["mode"], "analysis-only")

    def test_extract_only_and_required_output_columns(self):
        code, out, _ = run([EXAMPLE, "--extract-only"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["candidates"][0]["spread_derived"], 0.2)
        _, out, _ = run([EXAMPLE, "--now", "2026-11-06T15:15:00Z"])
        self.assertIn("Instrument | LONG / SHORT / KEIN TRADE | Qualitaet des Setups | moeglicher Trigger | Stop | Ziel | Begruendung", out)

    def test_invalid_json_and_wrong_input_shape_return_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "bad.json")
            for text in ("not JSON", "[]", '{"source":"wrong"}', '{"source":"Plus500 +Insights","value":NaN}'):
                with open(path, "w") as handle:
                    handle.write(text)
                code, _, err = run([path])
                self.assertEqual(code, 2)
                self.assertIn("sentiment:", err)

    def test_table_escapes_screenshot_text(self):
        report = {"note": "analysis", "candidates": [{"instrument": "A|B\nC", "decision": "KEIN TRADE", "quality": "offen",
                  "trigger": None, "stop": None, "target": None, "reasons": ["x|y\nz"]}]}
        self.assertIn("A/B C", sentiment.render(report))


if __name__ == "__main__":
    unittest.main()
