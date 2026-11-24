"""Synthetic regressions for the authorized joint OR/Base-High close rule."""
import copy
import datetime as dt
import unittest

from _load import load
from test_qullamaggie import (CFG as QCFG, base, catalyst, history, intrabars,
                             q, report, row as scanner_row)
from test_true_trades import CFG, OPEN, MarketData, document, iso, row
from true_trade_replay import QULL, replay


def joint_bars(base_high, span=1, minutes=1, offsets=(-0.25, 0.1)):
    """First OR cross stays above ORH throughout; no recross is necessary."""
    values = [(base_high - 0.6, base_high - 0.5,
               base_high - 0.8, base_high - 0.6)] * (span // minutes)
    previous = values[-1][3]
    for offset in offsets:
        close = base_high + offset
        values.append((previous, max(previous, close) + 0.05,
                       min(previous, close) - 0.05, close))
        previous = close
    return intrabars(values, minutes)


class JointCloseScannerTests(unittest.TestCase):
    def setUp(self):
        self.daily = base()
        self.high = q.breakout(self.daily, {"1M": 100, "3M": 100, "6M": 100}, QCFG)[1]["base_high"]

    def scan(self, bars, now=None):
        return scanner_row(report(q.MODES[0], self.daily, bars, now=now or bars[-1]["end"]))

    def test_later_joint_close_counts_for_every_resolvable_opening_range(self):
        for span, minutes in ((1, 1), (5, 1), (60, 1), (5, 5), (60, 5)):
            with self.subTest(span=span, minutes=minutes):
                bars = joint_bars(self.high, span, minutes)
                level = max(b["high"] for b in bars[:span // minutes])
                self.assertTrue(all(b["close"] > level for b in bars[span // minutes:]))
                result = self.scan(bars)
                matches = [e for e in result["observed_triggers"] if e["type"] == "ORH_%dm" % span]
                self.assertEqual(len(matches), 1)
                event = matches[0]
                self.assertEqual(event["at"], iso(bars[-1]["end"]))
                self.assertEqual(event["index"], len(bars) - 1)
                self.assertGreater(event["price"], result["metrics"]["base_high"])
                self.assertEqual(result["setup_status"], "TRIGGERED")

    def test_close_equal_to_base_high_does_not_confirm(self):
        result = self.scan(joint_bars(self.high, offsets=(0.0,)))
        self.assertEqual(result["setup_status"], "SETUP_READY")
        self.assertIsNone(result["trigger"])
        self.assertEqual(result["observed_triggers"], [])

    def test_equal_close_does_not_consume_a_later_joint_confirmation(self):
        bars = joint_bars(self.high, offsets=(0.0, 0.1))
        result = self.scan(bars)
        self.assertIsNotNone(result["trigger"])
        self.assertEqual(result["trigger"]["at"], iso(bars[-1]["end"]))

    def test_no_joint_close_leaves_setup_ready_without_breakout(self):
        result = self.scan(joint_bars(self.high, offsets=(-0.25, -0.1)))
        self.assertEqual(result["setup_status"], "SETUP_READY")
        self.assertIsNone(result["trigger"])

    def test_first_already_qualifying_close_retains_previous_result(self):
        bars = joint_bars(self.high, offsets=(0.1, 0.2))
        previous_events = [e for e in q.triggers(bars, "buy", QCFG) if e["price"] > self.high]
        result = self.scan(bars)
        self.assertEqual(result["observed_triggers"], previous_events)
        self.assertEqual(result["trigger"]["at"], iso(bars[1]["end"]))

    def test_no_joint_confirmation_across_a_missing_bar(self):
        bars = joint_bars(self.high)
        del bars[1]
        result = self.scan(bars)
        self.assertIsNone(result["trigger"])
        self.assertEqual(result["setup_status"], "SETUP_READY")
        self.assertEqual(result["observed_triggers"], [])

    def test_joint_confirmation_requires_the_later_completed_bar(self):
        bars = joint_bars(self.high)
        for now in (bars[0]["end"], bars[-1]["end"] - dt.timedelta(seconds=1)):
            with self.subTest(now=now):
                self.assertIsNone(self.scan(bars, now)["trigger"])
        result = self.scan(bars)
        self.assertIsNotNone(result["trigger"])
        self.assertEqual(result["trigger"]["at"], iso(bars[-1]["end"]))

    def test_ep_keeps_first_or_close_without_breakout_base_filter(self):
        bars = intrabars([(110, 111, 109, 110), (110, 113, 110, 112), (112, 115, 111, 114)])
        result = scanner_row(report(q.MODES[1], history(), bars, events=[catalyst()], now=bars[-1]["end"]))
        self.assertEqual(result["setup_status"], "TRIGGERED")
        self.assertEqual(result["trigger"]["at"], iso(bars[1]["end"]))
        self.assertEqual(result["trigger"]["price"], 112)

    def test_parabolic_long_keeps_first_or_and_first_green(self):
        daily = history([50.0] * 175 + [60, 80, 130, 200, 300] + [250, 180, 140, 120])
        bars = intrabars([(100, 101, 99, 100), (100, 103, 100, 102), (102, 105, 101, 104)], 5)
        result = scanner_row(report(q.MODES[2], daily, bars, now=bars[-1]["end"]), "PARABOLIC_LONG")
        self.assertEqual(result["setup_status"], "TRIGGERED")
        self.assertEqual(result["trigger"]["at"], iso(bars[1]["end"]))
        self.assertEqual({e["type"] for e in result["observed_triggers"]}, {"ORH_5m", "FIRST_GREEN_5m"})

    def test_parabolic_short_keeps_first_or_close(self):
        daily = history([50.0] * 175 + [60, 80, 130, 200, 300])
        bars = intrabars([(300, 302, 299, 301), (301, 301, 294, 295), (295, 296, 289, 290)])
        result = scanner_row(report(q.MODES[2], daily, bars, now=bars[-1]["end"]), "PARABOLIC_SHORT")
        self.assertEqual(result["setup_status"], "TRIGGERED")
        self.assertEqual(result["trigger"]["at"], iso(bars[1]["end"]))
        self.assertEqual(result["trigger"]["price"], 295)


class JointCloseReplayTests(unittest.TestCase):
    def case(self, minutes=5, offsets=(-0.25, 0.1)):
        doc = document()
        market = MarketData(doc)
        daily = {s: market.series(s, "1d", OPEN) for s in market.universe}
        before = next(r for r in QULL.scan(daily, market.universe, OPEN, market.clock, QULL.MODES[0])[0]
                      if r["symbol"] == "DEMO")
        self.assertEqual(before["setup_status"], "SETUP_READY")
        high = before["metrics"]["base_high"]
        bars = joint_bars(high, minutes, minutes, offsets)
        records = []
        for i, b in enumerate(bars):
            raw = row(i, tuple(b[k] for k in ("open", "high", "low", "close")), minutes, volume=600000)
            records.append({k: v for k, v in raw.items() if not k.startswith("_") and k != "interval"})
        doc["bars"]["DEMO"] = {"1d": doc["bars"]["DEMO"]["1d"], str(minutes) + "m": records}
        return doc, high

    def test_scanner_replay_and_confluence_agree_on_later_joint_close(self):
        confluence = load("trader-confluence", "confluence")
        for minutes in (1, 5):
            with self.subTest(minutes=minutes):
                doc, high = self.case(minutes)
                market = MarketData(doc)
                frame = str(minutes) + "m"
                at = doc["bars"]["DEMO"][frame][-1]["end"]
                now = OPEN + dt.timedelta(minutes=3 * minutes)
                daily = {s: market.series(s, "1d", OPEN) for s in market.universe}
                native = QULL.scan(daily, market.universe, now, market.clock, QULL.MODES[0],
                                   intraday={"DEMO": {frame: market.series("DEMO", frame, now)}})[0]
                scanner = next(r for r in native if r["symbol"] == "DEMO")
                self.assertEqual(scanner["setup_status"], "TRIGGERED")
                self.assertEqual(scanner["trigger"]["at"], at)
                self.assertGreater(scanner["trigger"]["price"], high)
                reconstructed = replay(market, CFG, "2026-11-09", "2026-11-09")
                parents = [p for p in reconstructed["plans"] if p["strategy"] == "BREAKOUT"]
                self.assertEqual(len(parents), 1)
                parent = parents[0]
                for key in ("at", "price", "level", "type"):
                    self.assertEqual(parent[{"at": "trigger_time"}.get(key, "trigger_" + key)], scanner["trigger"][key])
                children = [p for p in reconstructed["plans"] if p["strategy"] == "CONFLUENCE_TRIGGERED"]
                self.assertEqual(len(children), 1)
                self.assertEqual(children[0]["parent_breakout_id"], parent["id"])
                self.assertEqual(children[0]["trigger_time"], at)
                self.assertEqual(children[0]["trigger_price"], parent["trigger_price"])
                report_doc = dict(created_by="trader-scanner", mode=QULL.MODES[0], as_of=at, results=native)
                accepted = confluence.analyze({"decision_at": at, "reports": [report_doc]})
                result = next(r for r in accepted["results"] if r["ticker"] == "DEMO")
                self.assertEqual(result["qullamaggie_status"]["BREAKOUT"], "TRIGGERED")
                self.assertEqual(result["trigger"]["BREAKOUT"]["at"], at)

    def test_replay_strict_base_equality_no_joint_close_and_original_positive(self):
        for offsets, expected in (((0.0,), False), ((-0.25, -0.1), False), ((0.1, 0.2), True), ((0.0, 0.1), True)):
            with self.subTest(offsets=offsets):
                doc, _ = self.case(offsets=offsets)
                result = replay(MarketData(doc), CFG, "2026-11-09", "2026-11-09")
                audit = next(a for a in result["audit"] if a["strategy"] == "BREAKOUT" and a["ticker"] == "DEMO")
                self.assertEqual(audit["triggered"], expected)
                if expected:
                    index = 1 if offsets[0] > 0 else 2
                    self.assertEqual(audit["trigger_time"], doc["bars"]["DEMO"]["5m"][index]["end"])

    def test_replay_never_confirms_across_gap_or_late_availability(self):
        for minutes in (1, 5):
            for issue in ("gap", "late_first_cross", "late_joint_close"):
                with self.subTest(minutes=minutes, issue=issue):
                    doc, _ = self.case(minutes)
                    rows = doc["bars"]["DEMO"][str(minutes) + "m"]
                    if issue == "gap":
                        del rows[1]
                    else:
                        i = 1 if issue == "late_first_cross" else 2
                        rows[i]["available_at"] = iso(OPEN + dt.timedelta(minutes=(i + 1) * minutes, seconds=1))
                    result = replay(MarketData(doc), CFG, "2026-11-09", "2026-11-09")
                    audit = next(a for a in result["audit"] if a["strategy"] == "BREAKOUT" and a["ticker"] == "DEMO")
                    self.assertTrue(audit["setup_ready"])
                    self.assertFalse(audit["triggered"])
                    self.assertFalse(any(p["strategy"] in ("BREAKOUT", "CONFLUENCE_TRIGGERED") for p in result["plans"]))

    def test_gapped_1m_opening_still_has_priority_over_complete_5m(self):
        original = document()
        doc, _ = self.case(minutes=1)
        doc["bars"]["DEMO"]["5m"] = copy.deepcopy(original["bars"]["DEMO"]["5m"])
        del doc["bars"]["DEMO"]["1m"][1]
        result = replay(MarketData(doc), CFG, "2026-11-09", "2026-11-09")
        coverage = next(c for c in result["coverage"] if c["ticker"] == "DEMO")
        self.assertEqual(coverage["frame"], "1m")
        self.assertEqual(coverage["contiguous_opening_bars"], 1)
        self.assertFalse(any(p["strategy"] == "BREAKOUT" for p in result["plans"]))


if __name__ == "__main__":
    unittest.main()
