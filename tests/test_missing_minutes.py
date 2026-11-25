"""Small synthetic wire captures; real PD evidence stays outside the repo."""
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import unittest

from _load import ROOT
sys.path.insert(0, str(Path(ROOT) / "trader-backtest"))
from intraday_providers.missing_minutes import (BAR_PRESENT, GAP_UNEXPLAINED,
    NO_BAR_CONSISTENT_WITH_TRADE_CONDITIONS, RULE_URL, RawResponse, RuleEvidence, classify_minute)

START, END = "2026-11-09T14:31:00Z", "2026-11-09T14:32:00Z"
WIRE_END = "2026-11-09T14:31:59.999999999Z"
# Reviewed rule excerpts only; no real market records or PD prices.
TEXT = "\n".join([
    "Minute and daily bars are aggregated from trades.",
    "` `  | Regular Sale  | AB  | MD  | 🟢  | 🟢  | 🟢",
    "`@`  | Regular Sale  | CO  | MD  | 🟢  | 🟢  | 🟢",
    "`F`  | Intermarket Sweep  | ABC  | MD  | 🟢  | 🟢  | 🟢",
    "`I`  | Odd Lot Trade  | ABCO  | MD  | 🔴  | 🔴  | 🟢",
    "A trade can have more than one condition. In this case the strictest rule is applied.",
    "Finally, the bar is only emitted if none of its fields (open, high, low, close, volume) are 0.",
])


def sha(body):
    return hashlib.sha256(body).hexdigest()


def rule(text=TEXT):
    return RuleEvidence(RULE_URL, text, sha(text.encode()), "2026-11-10T12:00:00Z")


def trade(codes=None, **changes):
    return dict(dict(t="2026-11-09T14:31:10.000000001Z", p=100., s=2,
                     i=1, x="D", z="A", c=[" ", "I"] if codes is None else codes), **changes)


def capture(kind="trades", rows=None, params=None, payload=None):
    query = dict(start=START, end=WIRE_END, feed="sip", sort="asc", limit=10000)
    if kind == "bars":
        query.update(start="2026-11-09T14:30:00Z", end="2026-11-09T14:34:59.999999999Z",
                     timeframe="1Min", adjustment="raw", limit=10)
    query.update(params or {})
    body = json.dumps(payload if payload is not None else dict(symbol="DEMO", next_page_token=None,
        **{kind: rows if rows is not None else [trade()] if kind == "trades" else []})).encode()
    return RawResponse("ALPACA", "SIP", "https://data.alpaca.markets/v2/stocks/DEMO/" + kind,
                       query, body, sha(body), 200, "2026-11-10T12:00:00Z", len(body), True)


def diagnose(bars=None, trades=None, source=None, **changes):
    args = dict(provider="ALPACA", feed="SIP", ticker="DEMO", start=START, end=END,
                bars_response=capture("bars") if bars is None else bars,
                trades_response=capture() if trades is None else trades,
                rule_evidence=rule() if source is None else source)
    args.update(changes)
    return classify_minute(**args)


class MissingMinuteTests(unittest.TestCase):
    def unexplained(self, result, reason=None):
        self.assertEqual(result["state"], GAP_UNEXPLAINED)
        self.assertTrue(result["reconstruction_blocking"])
        self.assertFalse(result["may_bridge_gap"])
        self.assertEqual(result["bars_created"], 0)
        if reason:
            self.assertIn(reason, result["reasons"])

    def test_present_bar_needs_no_trade_or_rule_explanation(self):
        bar = dict(t=START, o=100, h=101, l=99, c=100, v=10)
        result = diagnose(bars=capture("bars", [bar]), trades_response=None, rule_evidence=None)
        self.assertEqual(result["state"], BAR_PRESENT)
        self.assertFalse(result["native_admission_assessed"])
        self.assertEqual(result["bars_created"], 0)

    def test_observed_bar_in_partial_page_is_not_a_missing_bar(self):
        body = dict(symbol="DEMO", bars=[dict(t=START)], next_page_token="another-page")
        result = diagnose(bars=capture("bars", payload=body))
        self.assertEqual(result["state"], BAR_PRESENT)
        self.assertFalse(result["evidence"]["bars"]["complete_in_api_scope"])

    def test_absent_bar_without_evidence_is_unexplained(self):
        self.unexplained(classify_minute("ALPACA", "SIP", "DEMO", START, END))

    def test_matching_odd_lots_are_explained_but_still_blocking(self):
        result = diagnose()
        self.assertEqual(result["state"], NO_BAR_CONSISTENT_WITH_TRADE_CONDITIONS)
        self.assertTrue(result["reconstruction_blocking"])
        self.assertFalse(result["may_bridge_gap"])
        self.assertEqual(result["bars_created"], 0)
        self.assertEqual(result["evidence"]["trades"]["raw_sha256"], capture().raw_sha256)
        self.assertEqual(result["evidence"]["rule"]["source_url"], RULE_URL)
        self.assertEqual(result["historical_pit_availability"], "NOT_ESTABLISHED")

    def test_mixed_conditions_apply_strictest_matching_rule(self):
        rows = [trade([" ", "I"]), trade([" ", "F", "I"], i=2)]
        result = diagnose(trades=capture(rows=rows))
        self.assertEqual(result["state"], NO_BAR_CONSISTENT_WITH_TRADE_CONDITIONS)
        self.assertEqual(result["evidence"]["trades"]["condition_counts"], {" ": 2, "I": 2, "F": 1})

    def test_one_price_updating_trade_prevents_explanation(self):
        rows = [trade(), trade([" "], i=2)]
        self.unexplained(diagnose(trades=capture(rows=rows)), "PRICE_UPDATING_TRADE_OBSERVED")

    def test_unknown_condition_is_not_overruled_by_odd_lot(self):
        self.unexplained(diagnose(trades=capture(rows=[trade(["I", "?"])])), "UNKNOWN_OR_UNSUPPORTED_CONDITION")

    def test_condition_for_wrong_tape_is_unexplained(self):
        self.unexplained(diagnose(trades=capture(rows=[trade(["@", "I"])])), "CONDITION_TAPE_MISMATCH")

    def test_nasdaq_regular_sale_and_odd_lot_use_matching_tape(self):
        result = diagnose(trades=capture(rows=[trade(["@", "I"], z="C")]))
        self.assertEqual(result["state"], NO_BAR_CONSISTENT_WITH_TRADE_CONDITIONS)

    def test_unknown_tape_is_unexplained(self):
        self.unexplained(diagnose(trades=capture(rows=[trade(z="O")])), "UNSUPPORTED_TAPE")

    def test_pagination_token_prevents_explanation(self):
        data = dict(symbol="DEMO", trades=[trade()], next_page_token="next")
        self.unexplained(diagnose(trades=capture(payload=data)), "TRADES_PAGINATION_INCOMPLETE_OR_UNKNOWN")

    def test_missing_pagination_field_does_not_prove_completeness(self):
        self.unexplained(diagnose(trades=capture(payload=dict(symbol="DEMO", trades=[trade()]))), "TRADES_PAGINATION_INCOMPLETE_OR_UNKNOWN")

    def test_later_page_without_previous_pages_is_unexplained(self):
        self.unexplained(diagnose(trades=capture(params=dict(page_token="previous-cursor"))), "TRADES_PAGINATION_INCOMPLETE_OR_UNKNOWN")

    def test_truncated_or_length_unknown_response_is_unexplained(self):
        for change in (dict(body_complete=False), dict(content_length=0), dict(content_length=None)):
            with self.subTest(change=change):
                self.unexplained(diagnose(trades=replace(capture(), **change)), "TRADES_BODY_COMPLETENESS_NOT_ESTABLISHED")

    def test_hash_is_checked_against_actual_stored_bytes(self):
        self.unexplained(diagnose(trades=replace(capture(), raw_sha256="0" * 64)), "TRADES_RAW_HASH_MISMATCH")

    def test_capture_before_minute_end_cannot_explain_a_missing_bar(self):
        self.unexplained(diagnose(trades=replace(capture(), retrieved_at="2026-11-09T14:31:30Z")), "TRADES_INTERVAL_NOT_ENDED_AT_CAPTURE")
        self.unexplained(diagnose(bars=replace(capture("bars"), retrieved_at="2026-11-09T14:31:30Z")), "BARS_INTERVAL_NOT_ENDED_AT_CAPTURE")

    def test_malformed_json_and_duplicate_json_keys_are_unexplained(self):
        for body in (b'{"symbol":', b'{"symbol":"DEMO","symbol":"DEMO","trades":[],"next_page_token":null}'):
            with self.subTest(body=body):
                self.unexplained(diagnose(trades=replace(capture(), body=body, raw_sha256=sha(body), content_length=len(body))))

    def test_wrong_feed_or_provider_is_not_mixed(self):
        for change in (dict(feed="IEX"), dict(provider="YAHOO")):
            with self.subTest(change=change):
                self.unexplained(diagnose(trades=replace(capture(), **change)), "TRADES_SOURCE_IDENTITY_MISMATCH")
        self.unexplained(diagnose(trades=capture(params=dict(feed="iex"))))

    def test_wrong_or_broader_trade_interval_is_unexplained(self):
        for params in (dict(start="2026-11-09T14:30:00Z"), dict(end=END), dict(end="2026-11-09T14:31:30Z")):
            with self.subTest(params=params):
                self.unexplained(diagnose(trades=capture(params=params)), "TRADES_EXACT_TRADE_INTERVAL_REQUIRED")

    def test_trade_at_exclusive_end_is_outside_request(self):
        self.unexplained(diagnose(trades=capture(rows=[trade(t=END)])), "TRADES_RECORD_OUTSIDE_REQUEST")

    def test_nanosecond_boundaries_and_explicit_ny_offset(self):
        rows = [trade(t=START), trade(i=2, t=WIRE_END)]
        result = diagnose(trades=capture(rows=rows), start="2026-11-09T09:31:00-05:00", end="2026-11-09T09:32:00-05:00")
        self.assertEqual(result["state"], NO_BAR_CONSISTENT_WITH_TRADE_CONDITIONS)
        self.assertEqual(result["interval"]["start"], START)

    def test_empty_trade_response_alone_never_explains_absence(self):
        self.unexplained(diagnose(trades=capture(rows=[])), "EMPTY_TRADES_DO_NOT_PROVE_NO_TRADING")

    def test_missing_or_wrong_rule_provenance_is_unexplained(self):
        self.unexplained(diagnose(rule_evidence=None), "RULE_SOURCE_MISSING")
        for source in (replace(rule(), text_sha256="0" * 64), replace(rule(), source_url="https://example.test/rule"), rule(TEXT.replace("Odd Lot Trade", "Automatic Execution"))):
            with self.subTest(source=source):
                self.unexplained(diagnose(source=source), "RULE_SOURCE_UNSUPPORTED_OR_UNBOUND")

    def test_incomplete_bars_capture_does_not_establish_absence(self):
        data = dict(symbol="DEMO", bars=[], next_page_token="next")
        self.unexplained(diagnose(bars=capture("bars", payload=data)), "BARS_PAGINATION_INCOMPLETE_OR_UNKNOWN")

    def test_wrong_bar_identity_cannot_be_explained_by_matching_trades(self):
        self.unexplained(diagnose(bars=capture("bars", payload=dict(symbol="OTHER", bars=[], next_page_token=None))), "BARS_RESPONSE_IDENTITY_OR_SCHEMA_MISMATCH")
        self.unexplained(diagnose(bars=capture("bars", rows=[dict(t=START)], params=dict(timeframe="5Min"))), "BARS_BAR_INTERVAL_OR_CONVENTION_MISMATCH")

    def test_invalid_trade_or_duplicate_record_is_unexplained(self):
        for rows in ([trade(c=[])], [trade(p=0)], [trade(p=10 ** 400)], [trade(), trade()]):
            with self.subTest(rows=rows):
                self.unexplained(diagnose(trades=capture(rows=rows)))

    def test_unsupported_source_and_http_error_do_not_fetch(self):
        self.unexplained(diagnose(feed="IEX"))
        self.unexplained(diagnose(trades=replace(capture(), http_status=403)), "TRADES_HTTP_NOT_200")

    def test_inputs_are_not_modified_and_no_bars_are_returned(self):
        bars, trades = capture("bars"), capture()
        before = copy.deepcopy((bars, trades))
        result = diagnose(bars=bars, trades=trades)
        self.assertEqual((bars, trades), before)
        self.assertNotIn("bars", result)
        self.assertFalse(result["may_bridge_gap"])

    def test_different_asof_identity_conventions_are_not_combined(self):
        self.unexplained(diagnose(trades=capture(params=dict(asof="2026-11-06"))), "ASOF_IDENTITY_CONVENTION_MISMATCH")

    def test_naive_or_unaligned_target_is_rejected(self):
        for start in ("2026-11-09T14:31:00", "2026-11-09T14:31:00.000000001Z"):
            with self.subTest(start=start), self.assertRaises(ValueError):
                diagnose(start=start)

    def test_explained_gap_does_not_change_native_prefix_frame_or_triggers(self):
        from test_true_trades import document, row, OPEN, CFG, MarketData, replay, session_rows, contiguous_prefix
        doc = document()
        records = [row(0, minutes=1), row(2, (106, 107, 105, 106.8), minutes=1)]
        doc["bars"]["DEMO"]["1m"] = [{k: v for k, v in r.items() if not k.startswith("_") and k != "interval"} for r in records]
        original = copy.deepcopy(doc)
        before = replay(MarketData(doc), CFG, "2026-11-09", "2026-11-09")
        evidence = capture("bars", [dict(t=r["start"], **{k: r[k] for k in ("o", "h", "l", "c", "v")}) for r in records])
        result = diagnose(bars=evidence)
        self.assertEqual(result["state"], NO_BAR_CONSISTENT_WITH_TRADE_CONDITIONS)
        self.assertTrue(result["reconstruction_blocking"])
        after = replay(MarketData(doc), CFG, "2026-11-09", "2026-11-09")
        self.assertEqual(before, after)
        self.assertEqual(doc, original)
        self.assertEqual(before["plans"], [])
        frame, rows = session_rows(MarketData(doc), "DEMO", "2026-11-09")
        self.assertEqual(frame, "1m")  # Complete 5m still cannot replace a gapped 1m opening.
        self.assertEqual(len(contiguous_prefix(rows, OPEN)), 1)


if __name__ == "__main__":
    unittest.main()
