"""Offline, causal OHLC scenarios. Every price/catalyst here is synthetic."""

import copy
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from _load import load, ROOT

scan = load("trader-scanner", "scan")
q = load("trader-scanner", "qullamaggie")
risk = load("trader-risk", "check")
CFG = q.config()
NOW = scan.parse_now("2026-11-09 09:00")
LIVE = scan.parse_now("2026-11-09 09:50")


def days(n=180):
    day, out = dt.date(2026, 11, 6), []
    while len(out) < n:
        if scan.session(day):
            out.append(day)
        day -= dt.timedelta(days=1)
    return list(reversed(out))


def stamp(local):
    return (scan.eastern_to_utc(local) - dt.datetime(1970, 1, 1)).total_seconds()


def history(prices=None, volume=1000000):
    prices = prices or [100.0] * 180
    return [{"date": day, "open": price, "close": price, "high": price + 1,
             "low": price - 1, "volume": volume} for day, price in zip(days(len(prices)), prices)]


def series(bars):
    keys = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    return dict({key: [b[name] for b in bars] for key, name in keys.items()},
                t=[stamp(dt.datetime.combine(b["date"], dt.time(9, 30))) for b in bars],
                meta={"dataGranularity": "1d"}, price_basis="YAHOO_SPLIT_ADJUSTED", splits={})


def payload(sym, source):
    keys = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    return {"chart": {"result": [{"meta": dict(source["meta"], symbol=sym), "timestamp": source["t"],
           "indicators": {"quote": [{name: source[key] for key, name in keys.items()}]},
           "events": {"splits": source.get("splits", {})}}], "error": None}}


def base(n=20, contraction=True, weak=False):
    prefix = [50.0] * 80 + [50 + i * 50 / 59 for i in range(60)]
    if weak:
        prefix = [100.0] * 140
    bars = history(prefix + [100 + i * 0.03 for i in range(n)] + [106.0])
    for i, b in enumerate(bars[-n - 1:-1]):
        width = 5 - i * 4 / (n - 1) if contraction else 5
        b.update(high=b["close"] + width, low=b["close"] - width)
    return bars


def intrabars(values=None, minutes=1, volumes=600000):
    values = values or [(110, 111, 109, 110), (110, 113, 110, 112)] + [(112, 114, 111, 113)] * 18
    opening = dt.datetime(2026, 11, 9, 9, 30)
    out = []
    for i, (o, h, l, c) in enumerate(values):
        start = opening + dt.timedelta(minutes=i * minutes)
        out.append({"start": start, "end": scan.eastern_to_utc(start + dt.timedelta(minutes=minutes)),
                    "open": o, "high": h, "low": l, "close": c,
                    "volume": volumes, "minutes": minutes})
    return out


def intraseries(bars):
    keys = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    return dict({key: [b[name] for b in bars] for key, name in keys.items()}, t=[stamp(b["start"]) for b in bars],
                meta={"dataGranularity": str(bars[0]["minutes"]) + "m"}, price_basis="YAHOO_SPLIT_ADJUSTED", splits={})


def catalyst(category="earnings", published="2026-11-06T13:00:00Z", **kwargs):
    return dict({"symbol": "DEMO", "source_skill": "trader-news", "category": category,
                 "published_at": published, "available_at": published, "confirmed": True,
                 "reference": "https://example.test/filing", "summary": "synthetic catalyst"}, **kwargs)


def report(mode, bars=None, intra=None, events=None, now=NOW, **kwargs):
    data = {"DEMO": series(bars or history()), "SLOW": series(history([50.0] * 180))}
    return q.scan(data, list(data), now, scan, mode,
                  {"DEMO": intraseries(intra)} if intra else {}, {"events": events or []}, **kwargs)[0]


def row(rows, variant=None):
    return next(r for r in rows if r["symbol"] == "DEMO" and (variant is None or r["variant"] == variant))


class Breakout(unittest.TestCase):
    def evaluate(self, bars=None, ranks=None, cfg=None):
        return q.breakout(bars or base(), ranks or {"1M": 99, "3M": 50, "6M": 40}, cfg or CFG)

    def test_leader_1m(self):
        result = self.evaluate()
        self.assertEqual(result[0], "SETUP_READY")
        self.assertEqual(result[1]["leader_periods"], ["1M"])

    def test_leader_3m(self):
        self.assertEqual(self.evaluate(ranks={"1M": 0, "3M": 99, "6M": 0})[0], "SETUP_READY")

    def test_leader_6m(self):
        self.assertEqual(self.evaluate(ranks={"1M": 0, "3M": 0, "6M": 99})[0], "SETUP_READY")

    def test_multiple_leaders_improve_quality(self):
        self.assertGreater(self.evaluate(ranks={"1M": 99, "3M": 99, "6M": 99})[3], self.evaluate()[3])

    def test_no_leader(self):
        self.assertEqual(self.evaluate(ranks={"1M": 97.9})[0], "FAILED")

    def test_exact_leader_threshold(self):
        self.assertEqual(self.evaluate(ranks={"1M": 98})[0], "SETUP_READY")

    def test_prior_move_above_30_and_100_allowed(self):
        self.assertGreater(self.evaluate()[1]["prior_move_pct"], 100)
        self.assertEqual(self.evaluate()[0], "SETUP_READY")

    def test_prior_move_exact_30(self):
        bars = [{"low": 100, "high": 100}, {"low": 100, "high": 130}]
        self.assertAlmostEqual(q.impulse(bars), 30)

    def test_weak_prior_move(self):
        self.assertNotEqual(self.evaluate(base(weak=True))[0], "SETUP_READY")

    def test_valid_two_week_base(self):
        self.assertEqual(self.evaluate(base(10))[0], "SETUP_READY")

    def test_valid_eight_week_base(self):
        self.assertEqual(self.evaluate(base(40))[0], "SETUP_READY")

    def test_overlong_base(self):
        self.assertNotEqual(self.evaluate(base(65))[0], "SETUP_READY")

    def test_higher_lows(self):
        self.assertTrue(self.evaluate()[1]["higher_lows"])

    def test_range_contraction(self):
        self.assertLess(self.evaluate()[1]["range_contraction"], 0.85)

    def test_atr_adr_contraction(self):
        self.assertLess(self.evaluate()[1]["atr_contraction"], 1)
        self.assertLess(self.evaluate()[1]["adr_contraction"], 1)

    def test_no_contraction(self):
        self.assertNotEqual(self.evaluate(base(contraction=False))[0], "SETUP_READY")

    def test_sma_distances_are_transparent(self):
        metric = self.evaluate()[1]
        for n in (10, 20, 50):
            self.assertIn("distance_sma%d_pct" % n, metric)
            self.assertIn("sma%d_rising" % n, metric)

    def test_daily_breakout(self):
        self.assertTrue(self.evaluate()[1]["daily_breakout"])

    def test_missing_intraday_ready_without_trigger(self):
        result = row(report(q.MODES[0], base()))
        self.assertEqual(result["setup_status"], "SETUP_READY")
        self.assertIsNone(result["trigger"])
        self.assertFalse(result["daily_breakout"]["current_entry"])

    def test_orh_1m(self):
        result = row(report(q.MODES[0], base(), intrabars(), now=LIVE))
        self.assertEqual(result["trigger"]["type"], "ORH_1m")
        self.assertEqual(result["trigger"]["at"], "2026-11-09T14:32:00Z")

    def test_orh_5m(self):
        bars = intrabars([(110, 111, 109, 110), (110, 114, 110, 113)], minutes=5)
        result = row(report(q.MODES[0], base(), bars, now=scan.parse_now("2026-11-09 09:40")))
        self.assertEqual(result["trigger"]["type"], "ORH_5m")

    def test_lost_base_support(self):
        bars = base()
        bars[-1].update(close=80, low=79, high=81, open=80)
        self.assertEqual(self.evaluate(bars)[0], "FAILED")


class EpisodicPivot(unittest.TestCase):
    def bars(self, opening=110, volume=3000000):
        bars = history()
        bars[-1].update(open=opening, close=opening + 2, high=opening + 3, low=opening - 1, volume=volume)
        return bars

    def evaluate(self, bars=None, catalysts=None, intra=None, cfg=None):
        return q.ep(bars or self.bars(), intra or [], [], catalysts if catalysts is not None else [catalyst()], cfg or CFG)

    def test_gap_exact_10(self):
        self.assertEqual(self.evaluate()[0], "SETUP_READY")
        self.assertAlmostEqual(self.evaluate()[1]["gap_pct"], 10)

    def test_gap_under_10(self):
        self.assertEqual(self.evaluate(self.bars(109.99))[0], "FAILED")

    def test_gap_configurable(self):
        self.assertEqual(self.evaluate(self.bars(105), cfg=q.config({"gap_pct": 5}))[0], "SETUP_READY")

    def test_high_relative_volume(self):
        self.assertEqual(self.evaluate()[1]["rvol"], 3)

    def test_low_volume_only_candidate(self):
        self.assertEqual(self.evaluate(self.bars(volume=1100000))[0], "CANDIDATE")

    def test_missing_volume(self):
        self.assertEqual(self.evaluate(self.bars(volume=0))[0], "INSUFFICIENT_DATA")

    def test_missing_open(self):
        bars = self.bars()
        bars[-1]["open"] = None
        self.assertEqual(self.evaluate(bars)[0], "INSUFFICIENT_DATA")

    def test_earnings_catalyst(self):
        r = row(report(q.MODES[1], self.bars(), events=[catalyst()]))
        self.assertEqual(r["setup_status"], "SETUP_READY")
        self.assertEqual(r["catalyst"][0]["category"], "earnings")

    def test_guidance_catalyst(self):
        self.assertEqual(row(report(q.MODES[1], self.bars(), events=[catalyst("guidance")]))["setup_status"], "SETUP_READY")

    def test_contract_catalyst(self):
        self.assertEqual(row(report(q.MODES[1], self.bars(), events=[catalyst("contract")]))["setup_status"], "SETUP_READY")

    def test_no_confirmed_catalyst(self):
        r = row(report(q.MODES[1], self.bars(), events=[catalyst(confirmed=False)]))
        self.assertEqual(r["setup_status"], "CANDIDATE")
        self.assertEqual(r["catalyst"], [])
        self.assertIsNone(r["trigger"])

    def test_no_automatic_entry_on_gap(self):
        r = row(report(q.MODES[1], self.bars(), events=[catalyst()]))
        self.assertIsNone(r["trigger"])
        self.assertEqual(r["action"], "WATCH")

    def test_orh_1m(self):
        r = row(report(q.MODES[1], history(), intrabars(), [catalyst()], LIVE))
        self.assertEqual(r["trigger"]["type"], "ORH_1m")
        self.assertEqual(r["setup_status"], "TRIGGERED")

    def test_orh_5m(self):
        intra = intrabars([(110, 111, 109, 110), (110, 114, 110, 113)], 5)
        r = row(report(q.MODES[1], history(), intra, [catalyst()], scan.parse_now("2026-11-09 09:40")))
        self.assertEqual(r["trigger"]["type"], "ORH_5m")

    def test_stop_low_of_day_at_trigger(self):
        r = row(report(q.MODES[1], history(), intrabars(), [catalyst()], LIVE))
        self.assertEqual(r["stop"]["price"], 109)
        self.assertEqual(r["stop"]["basis"], "LOW_OF_DAY")
        self.assertEqual(r["stop"]["as_of"], r["trigger"]["at"])

    def test_stop_atr_adr_warning(self):
        r = risk.scanner_stop_context(112, 109, "buy", 2, 2)
        self.assertFalse(r["preferred"])
        self.assertFalse(r["warning"])
        self.assertTrue(risk.scanner_stop_context(112.1, 109, "buy", 2, 2)["warning"])

    def test_strong_prior_move_is_quality_not_absolute_rule(self):
        bars = history([50 + i * 50 / 179 for i in range(180)])
        bars[-1].update(open=110, close=112, high=113, low=109, volume=3000000)
        result = self.evaluate(bars)
        self.assertEqual(result[0], "SETUP_READY")
        self.assertTrue(result[1]["already_extended"])
        self.assertLess(result[3], self.evaluate()[3])

    def test_missing_earnings_values_remain_absent(self):
        self.assertEqual(self.evaluate()[1]["earnings"], [])

    def test_provided_earnings_values_retained(self):
        event = catalyst(earnings={"eps_growth_pct": 35, "revenue_growth_pct": 25, "analyst_beat_pct": 10, "guidance": "raised"})
        r = row(report(q.MODES[1], self.bars(), events=[event]))
        self.assertEqual(r["metrics"]["earnings"][0]["eps_growth_pct"], 35)
        self.assertEqual(r["metrics"]["earnings_quality"]["points"], 10)
        plain = row(report(q.MODES[1], self.bars(), events=[catalyst()]))
        self.assertGreater(r["setup_quality"], plain["setup_quality"])

    def test_first_30_minutes_adv(self):
        result = self.evaluate(history(), intra=intrabars())
        self.assertGreaterEqual(result[1]["first_30m_adv_ratio"], 1)

    def test_extended_session_volume_context(self):
        ext = intrabars()[:2]
        result = q.ep(self.bars(), [], ext, [catalyst()], CFG)
        self.assertEqual(result[1]["extended_volume_adv_ratio"], 1.2)


class Parabolic(unittest.TestCase):
    def bars(self, small=False):
        return history([50.0] * 175 + ([60, 80, 130, 200, 300] if small else [55, 62, 69, 77, 85]))

    def evaluate(self, bars=None, cap=20000000000, variant="short"):
        return q.parabolic(bars or self.bars(), cap, CFG, variant)

    def test_large_cap_50_to_100(self):
        r = self.evaluate()
        self.assertEqual(r[0], "SETUP_READY")
        self.assertAlmostEqual(r[1]["extension_pct"], 70)

    def test_small_cap_300_plus(self):
        self.assertEqual(self.evaluate(self.bars(True), 500000000)[0], "SETUP_READY")

    def test_small_cap_does_not_use_large_cap_threshold(self):
        self.assertEqual(self.evaluate(cap=500000000)[0], "FAILED")

    def test_unknown_cap_explicit_conservative_category(self):
        r = self.evaluate(cap=None)
        self.assertEqual(r[1]["cap_category"], "UNKNOWN_OR_MID")
        self.assertEqual(r[1]["extension_threshold_pct"], 100)

    def test_five_strong_days(self):
        self.assertGreaterEqual(self.evaluate()[1]["strong_days"], 5)

    def test_three_strong_days(self):
        bars = history([50.0] * 177 + [60, 75, 90])
        self.assertEqual(self.evaluate(bars)[0], "SETUP_READY")

    def test_accelerating_trend_alternative(self):
        prices = [50.0] * 169 + [50 * (1.01 ** i) for i in range(6)]
        prices += [prices[-1] * (1.02 ** i) for i in range(1, 6)]
        metric = q.extension(history(prices), 20000000000, q.config({"large_extension_pct": 10}))
        self.assertTrue(metric["accelerating"])
        self.assertTrue(metric["overextended"])

    def test_orl_1m(self):
        bars = intrabars([(100, 102, 99, 101), (101, 101, 94, 95)])
        self.assertIn("ORL_1m", [e["type"] for e in q.triggers(bars, "sell", CFG, True)])

    def test_orl_5m(self):
        bars = intrabars([(100, 102, 99, 101), (101, 101, 94, 95)], 5)
        self.assertIn("ORL_5m", [e["type"] for e in q.triggers(bars, "sell", CFG, True)])

    def test_first_red_after_extension(self):
        bars = intrabars([(100, 102, 99, 101), (101, 108, 100, 107), (107, 109, 103, 104)], 5)
        event = next(e for e in q.triggers(bars, "sell", CFG, True) if e["type"] == "FIRST_RED_5m")
        self.assertEqual(event["at"], "2026-11-09T14:45:00Z")

    def test_red_without_extension_is_not_trigger(self):
        bars = intrabars([(100, 110, 99, 101), (101, 105, 100, 100.5)], 5)
        self.assertNotIn("FIRST_RED_5m", [e["type"] for e in q.triggers(bars, "sell", CFG, True)])

    def vwap_bars(self, reclaim=False):
        values = [(100, 102, 99, 101), (101, 101, 94, 95), (95, 100, 95, 99), (99, 99, 94, 95)]
        if reclaim:
            values.append((95, 110, 95, 109))
        return intrabars(values, 5, 1000)

    def test_vwap_failure(self):
        events = q.triggers(self.vwap_bars(), "sell", CFG, True)
        self.assertIn("VWAP_FAILURE", [e["type"] for e in events])

    def test_vwap_reclaim_invalidates_short(self):
        bars = self.vwap_bars(True)
        event = [e for e in q.triggers(bars, "sell", CFG, True) if e["type"] == "VWAP_FAILURE"]
        result = {"setup_status": "SETUP_READY", "variant": "PARABOLIC_SHORT", "setup": q.MODES[2], "reasons": []}
        q.apply_trigger(result, event, bars, [], CFG)
        self.assertEqual(result["setup_status"], "FAILED")
        self.assertIn("VWAP reclaimed after failure", result["reasons"])

    def test_no_vwap_without_volume(self):
        bars = self.vwap_bars()
        bars[0]["volume"] = None
        self.assertNotIn("VWAP_FAILURE", [e["type"] for e in q.triggers(bars, "sell", CFG, True)])

    def test_high_of_day_stop(self):
        bars = intrabars([(100, 102, 99, 101), (101, 101, 94, 95)], 5)
        r = {"setup_status": "SETUP_READY", "variant": "PARABOLIC_SHORT", "setup": q.MODES[2], "reasons": []}
        q.apply_trigger(r, q.triggers(bars, "sell", CFG, True), bars, [], CFG)
        self.assertEqual(r["stop"]["price"], 102)
        self.assertEqual(r["stop"]["basis"], "HIGH_OF_DAY")

    def test_mean_reversion_zones(self):
        r = row(report(q.MODES[2], self.bars(True)), "PARABOLIC_SHORT")
        self.assertEqual(r["mean_reversion_zones"]["sma10"], sum(b["close"] for b in self.bars(True)[-10:]) / 10)
        self.assertFalse(r["mean_reversion_zones"]["guaranteed_target"])

    def test_long_after_collapse(self):
        bars = self.bars(True) + []
        prices = [b["close"] for b in bars] + [250, 180, 140, 120]
        result = self.evaluate(history(prices), 500000000, "long")
        self.assertEqual(result[0], "SETUP_READY")
        self.assertGreaterEqual(result[1]["drawdown_pct"], 60)
        self.assertTrue(result[1]["successful_breakdown"])

    def test_collapse_without_prior_parabolic_rejected(self):
        self.assertEqual(self.evaluate(history([100.0] * 175 + [90, 80, 70, 60, 40]), variant="long")[0], "FAILED")

    def test_long_first_green_and_range_break(self):
        bars = intrabars([(100, 102, 99, 100), (100, 101, 90, 92), (92, 105, 91, 104)], 5)
        events = q.triggers(bars, "buy", CFG, True)
        self.assertIn("FIRST_GREEN_5m", [e["type"] for e in events])
        self.assertIn("ORH_5m", [e["type"] for e in events])

    def test_long_first_green_without_further_morning_decline(self):
        bars = intrabars([(100, 103, 99, 102)], 5)
        events = q.triggers(bars, "buy", CFG, True)
        self.assertEqual(events[0]["type"], "FIRST_GREEN_5m")
        self.assertEqual(events[0]["at"], "2026-11-09T14:35:00Z")

    def test_extension_alone_is_watch(self):
        r = row(report(q.MODES[2], self.bars(True)), "PARABOLIC_SHORT")
        self.assertEqual(r["setup_status"], "SETUP_READY")
        self.assertEqual(r["action"], "WATCH")
        self.assertIsNone(r["trigger"])

    def test_variants_remain_separate(self):
        rows = report(q.MODES[2], self.bars(True))
        self.assertEqual({r["variant"] for r in rows}, {"PARABOLIC_SHORT", "PARABOLIC_LONG"})


class Causality(unittest.TestCase):
    def test_future_daily_bar_ignored(self):
        source = series(history())
        future = dt.datetime(2026, 11, 9, 9, 30)
        source["t"].append(stamp(future))
        for key in ("o", "h", "l", "c"):
            source[key].append(100000)
        source["v"].append(100000000)
        bars, error = q.prepare(source, NOW, scan)
        self.assertIsNone(error)
        self.assertEqual(bars[-1]["close"], 100)

    def test_intraday_daily_end_data_ignored(self):
        source = series(history())
        source["t"].append(stamp(dt.datetime(2026, 11, 9, 9, 30)))
        for key in ("o", "h", "l", "c", "v"):
            source[key].append(999999)
        bars, error = q.prepare(source, LIVE, scan)
        self.assertIsNone(error)
        self.assertEqual(bars[-1]["close"], 100)

    def test_opening_range_not_completed(self):
        source = intraseries(intrabars())
        bars, _, error = q.intraday_bars(source, scan.parse_now("2026-11-09 09:30:59"), scan)
        self.assertIsNone(error)
        self.assertEqual(q.triggers(bars, "buy", CFG), [])

    def test_orh_requires_post_range_completed_bar(self):
        source = intraseries(intrabars())
        bars, _, _ = q.intraday_bars(source, scan.parse_now("2026-11-09 09:31:59"), scan)
        self.assertEqual(q.triggers(bars, "buy", CFG), [])

    def test_60m_range_needs_sixty_minutes(self):
        bars = intrabars([(100, 101, 99, 100)] * 60 + [(100, 105, 100, 104)])
        self.assertNotIn("ORH_60m", [e["type"] for e in q.triggers(bars[:60], "buy", CFG)])
        self.assertIn("ORH_60m", [e["type"] for e in q.triggers(bars, "buy", CFG)])

    def test_5m_does_not_fabricate_1m_range(self):
        bars = intrabars([(100, 101, 99, 100), (100, 105, 100, 104)], 5)
        self.assertNotIn("ORH_1m", [e["type"] for e in q.triggers(bars, "buy", CFG)])

    def test_missing_intraday_slot_disables_trigger(self):
        source = intraseries(intrabars())
        for key in ("t", "o", "h", "l", "c", "v"):
            del source[key][1]
        bars, _, error = q.intraday_bars(source, LIVE, scan)
        self.assertEqual(bars, [])
        self.assertIsNotNone(error)

    def test_missing_history(self):
        bars, error = q.prepare(series(history([100.0] * 126)), NOW, scan)
        self.assertEqual(bars, [])
        self.assertIn("127", error)

    def test_missing_daily_session(self):
        source = series(history())
        for key in ("t", "o", "h", "l", "c", "v"):
            del source[key][-10]
        self.assertIn("missing sessions", q.prepare(source, NOW, scan)[1])

    def test_stale_history(self):
        self.assertIn("stale", q.prepare(series(history()[:-1]), NOW, scan)[1])

    def test_unknown_price_basis(self):
        source = series(history())
        source["price_basis"] = "UNKNOWN"
        self.assertIsNotNone(q.prepare(source, NOW, scan)[1])

    def test_future_news_not_available(self):
        r = row(report(q.MODES[1], EpisodicPivot().bars(), events=[catalyst(published="2026-11-10T13:00:00Z")]))
        self.assertEqual(r["catalyst"], [])
        self.assertEqual(r["setup_status"], "CANDIDATE")

    def test_publication_before_observation_uses_later_time(self):
        event = catalyst(available_at="2026-11-10T13:00:00Z")
        self.assertIsNone(q.available(event, NOW))

    def test_unknown_publication_time_rejected(self):
        event = catalyst(published="2026-11-06")
        self.assertIsNone(q.available(event, NOW))

    def test_late_catalyst_does_not_confirm_early_entry(self):
        events = [catalyst(published="2026-11-09T14:49:00Z")]
        r = row(report(q.MODES[1], history(), intrabars(), events, LIVE))
        self.assertEqual(r["setup_status"], "SETUP_READY")
        self.assertIsNone(r["trigger"])

    def test_later_volume_cannot_confirm_earlier_entry(self):
        intra = intrabars(volumes=1000)
        intra[-1]["volume"] = 5000000
        r = row(report(q.MODES[1], history(), intra, [catalyst()], LIVE))
        self.assertEqual(r["setup_status"], "SETUP_READY")
        self.assertIsNone(r["trigger"])

    def test_last_session_no_current_trigger(self):
        past = intraseries(intrabars())
        now = scan.parse_now("2026-11-09 17:00")
        data = {"DEMO": series(history())}
        # No latest completed daily candle: fail rather than borrowing yesterday.
        r = q.scan(data, ["DEMO"], now, scan, q.MODES[1], {"DEMO": past})[0][0]
        self.assertIsNone(r["trigger"])

    def test_native_news_is_support_not_confirmed_cause(self):
        e = q.merge_evidence([{"checked": "2026-11-06T14:00:00Z", "items": [{"ticker": "DEMO", "time": "2026-11-06T13:00:00Z", "title": "earnings", "link": "https://example.test"}]}])
        self.assertFalse(e["events"][0]["confirmed"])

    def test_native_quiver_uses_disclosure_not_transaction(self):
        e = q.merge_evidence([{"created_by": "trader-quiver", "checked_at": "2026-11-06T14:00:00Z", "records": [{"ticker": "DEMO", "transaction_date": "2026-10-01", "disclosure_timestamp": "2026-11-06T13:00:00Z"}]}])
        self.assertEqual(e["events"][0]["published_at"], "2026-11-06T13:00:00Z")
        self.assertFalse(e["events"][0]["confirmed"])

    def test_native_social_uses_original_timestamp(self):
        e = q.merge_evidence([{"created_by": "trader-social", "checked_at": "2026-11-06T14:00:00Z", "results": [{"ticker": "DEMO", "original_timestamp": "2026-11-06T13:00:00Z"}]}])
        self.assertEqual(e["events"][0]["published_at"], "2026-11-06T13:00:00Z")

    def test_market_cap_requires_point_in_time_timestamp(self):
        e = {"metadata": [dict(catalyst(), market_cap=20000000000)]}
        self.assertEqual(q.latest_cap(e, "DEMO", NOW), 20000000000)
        e["metadata"][0]["available_at"] = "2026-12-01T00:00:00Z"
        self.assertIsNone(q.latest_cap(e, "DEMO", NOW))

    def test_future_snapshot_split_rebased(self):
        source = series(history())
        source["splits"] = {"future": {"date": stamp(dt.datetime(2026, 11, 10, 9, 30)), "numerator": 2, "denominator": 1}}
        bars, _ = q.prepare(source, NOW, scan)
        self.assertEqual(bars[-1]["close"], 200)
        self.assertEqual(bars[-1]["open"], 200)
        self.assertEqual(bars[-1]["volume"], 500000)


    def test_split_effective_today_matches_intraday_price_basis(self):
        source = series(history())
        source["splits"] = {"today": {"date": stamp(dt.datetime(2026, 11, 9, 9, 30)), "numerator": 2, "denominator": 1}}
        bars, error = q.prepare(source, LIVE, scan)
        self.assertIsNone(error)
        # The supplied Yahoo prices already reflect this 2:1 split. The shared
        # daily parser first rebases to Friday, then Q restores Monday's basis.
        self.assertEqual(bars[-1]["close"], 100)
        self.assertEqual(bars[-1]["open"], 100)
        self.assertEqual(bars[-1]["volume"], 1000000)

    def test_later_market_cap_cannot_classify_earlier_trigger(self):
        bars = history([50.0] * 175 + [55, 62, 69, 77, 85])
        intra = intrabars([(100, 102, 99, 101), (101, 101, 94, 95), (95, 97, 94, 96), (96, 97, 94, 96)], 5)
        data = {"DEMO": series(bars)}
        metadata = dict(catalyst(published="2026-11-09T14:49:00Z"), market_cap=20000000000)
        rows, _ = q.scan(data, ["DEMO"], LIVE, scan, q.MODES[2],
                         {"DEMO": intraseries(intra)}, {"metadata": [metadata]}, variant="short")
        self.assertIsNone(rows[0]["trigger"])
        metadata["published_at"] = metadata["available_at"] = "2026-11-09T14:00:00Z"
        earlier, _ = q.scan(data, ["DEMO"], LIVE, scan, q.MODES[2],
                            {"DEMO": intraseries(intra)}, {"metadata": [metadata]}, variant="short")
        self.assertEqual(earlier[0]["setup_status"], "TRIGGERED")

    def test_future_lod_does_not_move_initial_stop(self):
        intra = intrabars()
        intra[-1].update(low=108, close=110, high=114, open=113)
        r = row(report(q.MODES[1], history(), intra, [catalyst()], LIVE))
        self.assertEqual(r["stop"]["price"], 109)
        self.assertEqual(r["setup_status"], "FAILED")

    def test_future_hod_does_not_move_initial_short_stop(self):
        intra = intrabars([(100, 102, 99, 101), (101, 101, 94, 95), (95, 110, 94, 105)], 5)
        r = {"setup_status": "SETUP_READY", "variant": "PARABOLIC_SHORT", "setup": q.MODES[2], "reasons": []}
        q.apply_trigger(r, q.triggers(intra, "sell", CFG, True), intra, [], CFG)
        self.assertEqual(r["stop"]["price"], 102)
        self.assertEqual(r["setup_status"], "FAILED")

    def test_invalid_intraday_ohlc_disables_confirmation(self):
        source = intraseries(intrabars())
        source["h"][1] = 1
        bars, _, error = q.intraday_bars(source, LIVE, scan)
        self.assertEqual(bars, [])
        self.assertIsNotNone(error)

    def test_pre_market_never_forms_opening_range(self):
        source = intraseries(intrabars())
        source["t"] = [t - 7200 for t in source["t"]]
        bars, extended, error = q.intraday_bars(source, NOW, scan)
        self.assertEqual(bars, [])
        self.assertEqual(len(extended), 20)
        self.assertEqual(q.triggers(bars, "buy", CFG), [])

    def test_early_close_never_consumes_after_hours_bar(self):
        bars = intrabars([(100, 101, 99, 100)] * 43, minutes=5)
        source = intraseries(bars)
        # Friday after Thanksgiving closes at 13:00, not 16:00.
        source["t"] = [stamp(dt.datetime(2026, 11, 27, 9, 30) + dt.timedelta(minutes=5 * i)) for i in range(43)]
        completed, extended, error = q.intraday_bars(source, scan.parse_now("2026-11-27 13:05"), scan)
        self.assertIsNone(error)
        self.assertEqual(len(completed), 42)
        self.assertEqual(len(extended), 1)

    def test_conflicting_catalyst_never_confirms(self):
        r = row(report(q.MODES[1], EpisodicPivot().bars(), events=[catalyst(conflicting=True)]))
        self.assertEqual(r["catalyst"], [])
        self.assertEqual(r["setup_status"], "CANDIDATE")

    def test_stale_catalyst_excluded(self):
        r = row(report(q.MODES[1], EpisodicPivot().bars(), events=[catalyst(published="2026-10-01T13:00:00Z")]))
        self.assertEqual(r["catalyst"], [])


class RiskAndIntegration(unittest.TestCase):
    def test_scanner_never_grants_risk_approval(self):
        r = risk.scanner_stop_context(110, 109, "buy", 2, 2)
        self.assertEqual(r["verdict"], "NOT_ASSESSED")
        self.assertFalse(r["approval"])

    def test_short_default_permission_block(self):
        self.assertTrue(risk.scanner_stop_context(100, 101, "sell", 2, 2)["blocks"])

    def test_short_explicit_permissions(self):
        self.assertEqual(risk.scanner_stop_context(100, 101, "sell", 2, 2, cfg={"allow_short": True, "allow_margin": True})["blocks"], [])

    def test_stop_wrong_direction(self):
        self.assertTrue(risk.scanner_stop_context(100, 101, "buy", 2, 2)["blocks"])

    def test_config_cannot_relax_hard_risk(self):
        r = risk.scanner_stop_context(100, 110, "sell", 2, 2, 100)
        self.assertFalse(r["warning"])
        self.assertTrue(r["blocks"])
        self.assertFalse(r["approval"])

    def test_missing_stop_not_approved(self):
        r = risk.scanner_stop_context(100, None, "buy")
        self.assertFalse(r["approval"])
        self.assertIsNone(r["distance"])

    def test_nonfinite_stop_rejected(self):
        self.assertTrue(risk.scanner_stop_context(float("nan"), 100, "buy")["blocks"])

    def test_config_validation(self):
        for change in ({"base_min": 50, "base_max": 10}, {"gap_pct": float("nan")}, {"base_min": 10.1}, {"opening_ranges": [2]}, {"unknown": 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                q.config(change)

    def test_rank_ties(self):
        data = {"A": series(history()), "B": series(history())}
        rows, counts = q.scan(data, list(data), NOW, scan, q.MODES[0])
        self.assertEqual(rows[0]["ranks"], {"1M": 50, "3M": 50, "6M": 50})
        self.assertEqual(counts["ranked"]["6M"], 2)

    def test_ranks_different_returns(self):
        data = {"A": series(history([50 + i for i in range(180)])), "B": series(history())}
        rows, _ = q.scan(data, list(data), NOW, scan, q.MODES[0])
        self.assertEqual(next(r for r in rows if r["symbol"] == "A")["ranks"]["1M"], 100)

    def test_performance_windows_exported_without_rounding(self):
        bars = history([50 + i for i in range(180)])
        r = row(report(q.MODES[0], bars))
        self.assertAlmostEqual(r["performance_pct"]["1M"], (bars[-1]["close"] / bars[-22]["close"] - 1) * 100)
        self.assertAlmostEqual(r["performance_pct"]["3M"], (bars[-1]["close"] / bars[-64]["close"] - 1) * 100)
        self.assertAlmostEqual(r["performance_pct"]["6M"], (bars[-1]["close"] / bars[-127]["close"] - 1) * 100)

    def test_malformed_and_nonfinite_evidence_rejected(self):
        for doc in ({"items": [1]}, {"events": "unknown"}, {"metadata": [{"market_cap": float("nan")}] }):
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                q.merge_evidence([doc])

    def test_one_symbol_rank_unavailable(self):
        rows, _ = q.scan({"A": series(history())}, ["A"], NOW, scan, q.MODES[0])
        self.assertEqual(rows[0]["setup_status"], "INSUFFICIENT_DATA")

    def test_input_not_mutated(self):
        data = {"A": series(history()), "B": series(history())}
        before = copy.deepcopy(data)
        q.scan(data, list(data), NOW, scan, q.MODES[0])
        self.assertEqual(data, before)

    def cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = scan.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_all_modes_cli_and_separate_reports(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "charts.json")
            with open(source, "w") as fh:
                json.dump({s: payload(s, series(b)) for s, b in {"DEMO": base(), "SLOW": history()}.items()}, fh)
            for mode in q.MODES:
                target = os.path.join(folder, mode + ".json")
                code, out, err = self.cli([mode, "--offline", source, "--now", "2026-11-09 09:00", "--out", target, "--json"])
                self.assertEqual(err, "")
                doc = json.loads(out)
                self.assertEqual(doc["mode"], mode)
                self.assertTrue(doc["independent_of_minervini"])
                self.assertFalse(doc["trade_allowed"])
                self.assertTrue(os.path.isfile(target))

    def test_selection_after_ranking(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "charts.json")
            with open(source, "w") as fh:
                json.dump({s: payload(s, series(b)) for s, b in {"DEMO": base(), "SLOW": history()}.items()}, fh)
            _, out, _ = self.cli([q.MODES[0], "--offline", source, "--symbols", "DEMO", "--now", "2026-11-09 09:00", "--no-write", "--json"])
            doc = json.loads(out)
            self.assertEqual(doc["universe"], 2)
            self.assertEqual(doc["selected"], 1)
            self.assertEqual(doc["results"][0]["ranks"]["1M"], 100)

    def test_existing_modes_still_registered(self):
        for mode in ("daily", "orb", "minervini", "universe"):
            with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()):
                scan.main([mode, "--help"])
            self.assertEqual(cm.exception.code, 0)

    def test_offline_does_not_call_network(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "charts.json")
            with open(source, "w") as fh:
                json.dump({"DEMO": payload("DEMO", series(history()))}, fh)
            with patch.object(scan, "yahoo_get", side_effect=AssertionError("network forbidden")):
                code, out, err = self.cli([q.MODES[1], "--offline", source, "--now", "2026-11-09 09:00", "--no-write", "--json"])
            self.assertEqual(err, "")
            self.assertEqual(code, 1)

    def test_no_watchlist_or_risk_state_written(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "charts.json")
            with open(source, "w") as fh:
                json.dump({"DEMO": payload("DEMO", series(history()))}, fh)
            before = os.listdir(folder)
            self.cli([q.MODES[1], "--offline", source, "--now", "2026-11-09 09:00", "--no-write"])
            self.assertEqual(os.listdir(folder), before)


    def test_blocked_symbol_cannot_get_direction_label(self):
        r = row(report(q.MODES[1], history(), intrabars(), [catalyst()], LIVE, risk_cfg={"blocked": ["DEMO"]}))
        self.assertEqual(r["setup_status"], "TRIGGERED")
        self.assertEqual(r["action"], "WATCH")
        self.assertIn("symbol on existing risk blocked list", r["risk"]["blocks"])

    def test_live_fetch_keeps_full_rank_population(self):
        data = {"DEMO": series(base()), "SLOW": series(history())}
        with tempfile.TemporaryDirectory() as folder:
            universe = os.path.join(folder, "universe.txt")
            with open(universe, "w") as fh:
                fh.write("DEMO\nSLOW\n")
            with patch.object(scan, "fetch_qullamaggie", return_value=(data, {})) as fetch:
                _, out, err = self.cli([q.MODES[0], "--universe", universe, "--symbols", "DEMO", "--now", "2026-11-09 09:00", "--no-write", "--json"])
            self.assertEqual(err, "")
            self.assertEqual(fetch.call_args[0][0], ["DEMO", "SLOW"])
            doc = json.loads(out)
            self.assertEqual(doc["counts"]["ranked"]["1M"], 2)
            self.assertEqual(doc["selected"], 1)

    def test_missing_setup_config_is_explicit_error(self):
        code, _, err = self.cli([q.MODES[0], "--setup-config", "/tmp/no-such-qullamaggie-config.json", "--no-write"])
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)


if __name__ == "__main__":
    unittest.main()
