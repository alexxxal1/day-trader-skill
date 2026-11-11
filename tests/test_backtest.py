"""Offline numerical and adversarial time-integrity tests for the backtest layer."""
import copy
import datetime as dt
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from _load import ROOT

sys.path.insert(0, os.path.join(ROOT, "trader-backtest"))
from adapters import replay
from baseline import portfolio_baseline, signal_baseline
from common import Clock, calendar_2024_2027, iso, read, stamp
from data import MarketData
from engine import cost_scenarios, exit_price, friction, quantity, roundtrip_cost, simulate
from metrics import benchmark, distribution, forward_metrics, forward_samples, max_drawdown, portfolio_metrics, trade_metrics
from run import fingerprints, validate
from splits import partitions, select_plans, select_samples


def config():
    c = read(Path(ROOT) / "trader-backtest/config.first.json")
    c.update(start="2026-10-01", end="2026-10-09", universe=["A", "B"], max_positions=2,
             max_portfolio_risk=0.1, bootstrap_samples=50, randomizations=1000, flat_by="15:55")
    return c


def document():
    clock = Clock(calendar_2024_2027())
    days = [d for d in sorted(clock.sessions) if "2026-08-01" <= d <= "2026-11-06"]
    doc = {"universe": ["A", "B"], "sessions": clock.sessions, "bars": {}, "splits": {},
           "provenance": {"retrospective_snapshot": True}, "evidence": {"events": [], "metadata": []}}
    for sym, sign in (("A", 1), ("B", -1), ("SPY", 0.5), ("QQQ", 0.7)):
        rows = []
        for i, day in enumerate(days):
            start = clock.eastern_to_utc(dt.datetime.combine(dt.date.fromisoformat(day), dt.time(9, 30)))
            close = 100 + sign * i
            rows.append(dict(start=iso(start), end=iso(clock.close(day)), available_at=iso(clock.close(day)),
                             o=close, h=close + 2, l=close - 2, c=close, v=1000000))
        doc["bars"][sym] = {"1d": rows}
    return doc


def plan(sym="A", side="long", key="p", at="2026-10-01T13:30:00Z"):
    return {"id": key, "symbol": sym, "side": side, "strategy": "MINERVINI_ENTRY", "day": "2026-10-01",
            "at": at, "stop_known_at": at, "entry_ref": 100, "stop": 90 if side == "long" else 110,
            "target": 120 if side == "long" else 80, "RS_PROXY": 90, "liquidity": 1000, "confluence_level": 0}


def trading_document(prices=(100, 105, 110), sym="A"):
    doc = document()
    day = "2026-10-01"
    clock = Clock(doc["sessions"])
    start = clock.eastern_to_utc(dt.datetime(2026, 10, 1, 15, 40))
    rows = []
    for i, price in enumerate(prices):
        begin = start + dt.timedelta(minutes=i * 5)
        end = begin + dt.timedelta(minutes=5)
        rows.append(dict(start=iso(begin), end=iso(end), available_at=iso(end),
                         o=price, h=price + 1, l=price - 1, c=price, v=100000))
    for ticker in (sym, "B"):
        doc["bars"][ticker]["5m"] = copy.deepcopy(rows)
    return doc


class TemporalIntegrity(unittest.TestCase):
    def test_daily_unavailable_until_close(self):
        m = MarketData(document())
        now = stamp("2026-10-01T19:59:59Z")
        self.assertNotEqual(m.bars("A", "1d", now)[-1]["day"], "2026-10-01")
        self.assertEqual(m.bars("A", "1d", now + dt.timedelta(seconds=1))[-1]["day"], "2026-10-01")

    def test_bar_cannot_be_available_before_end(self):
        d = document()
        d["bars"]["A"]["1d"][0]["available_at"] = d["bars"]["A"]["1d"][0]["start"]
        with self.assertRaises(ValueError):
            MarketData(d)

    def test_corrections_do_not_rewrite_earlier_vintage(self):
        d = document()
        original = d["bars"]["A"]["1d"][0]
        changed = dict(original, c=999, h=1000, available_at="2026-11-06T22:00:00Z")
        d["bars"]["A"]["1d"].append(changed)
        m = MarketData(d)
        self.assertEqual(m.bars("A", "1d", stamp("2026-10-01T20:00:00Z"))[0]["c"], original["c"])
        self.assertEqual(m.bars("A", "1d", stamp("2026-11-06T23:00:00Z"))[0]["c"], 999)

    def test_news_social_disclosure_use_later_availability(self):
        d = document()
        event = dict(symbol="A", published_at="2026-10-01T12:00:00Z", available_at="2026-10-01T12:01:00Z")
        d["evidence"]["events"] = [dict(event, source_skill="trader-news"), dict(event, source_skill="trader-social"),
            dict(event, source_skill="trader-quiver", disclosure_timestamp="2026-10-02T12:00:00Z")]
        m = MarketData(d)
        self.assertEqual(m.events(stamp("2026-10-01T12:00:30Z"))["events"], [])
        self.assertEqual(len(m.events(stamp("2026-10-01T12:01:00Z"))["events"]), 2)
        self.assertEqual(len(m.events(stamp("2026-10-02T12:00:00Z"))["events"]), 3)

    def test_undated_and_transaction_only_disclosure_is_unavailable(self):
        d = document()
        d["evidence"]["events"] = [dict(source_skill="trader-quiver", published_at="2026-10-01T12:00:00Z", available_at="2026-10-01T12:00:00Z"),
                                  dict(source_skill="trader-news", published_at="2026-10-01", available_at="2026-10-01")]
        self.assertEqual(MarketData(d).events(stamp("2026-11-06T23:00:00Z"))["events"], [])

    def test_no_future_catalyst_revision(self):
        d = document()
        d["evidence"]["events"] = [dict(published_at="2026-10-01T12:00:00Z", available_at="2026-10-01T12:00:00Z", revised_at="2026-10-02T12:00:00Z")]
        self.assertEqual(MarketData(d).events(stamp("2026-10-01T15:00:00Z"))["events"], [])

    def test_complete_history_fails_with_missing_session(self):
        d = document()
        del d["bars"]["A"]["1d"][-3]
        m = MarketData(d)
        self.assertFalse(m.complete_history("A", stamp("2026-11-06T23:00:00Z"), 10))

    def test_missing_delisted_and_invalid_rows_are_audited(self):
        d = document()
        d["bars"]["B"]["1d"] = d["bars"]["B"]["1d"][:5]
        d["bars"]["A"]["1d"][1]["c"] = None
        m = MarketData(d)
        audit = m.audit("2026-10-01", "2026-10-09")
        self.assertIn("SURVIVORSHIP_BIAS_RISK", audit["flags"])
        self.assertTrue(audit["coverage"]["B"]["missing_sessions"])
        self.assertEqual(len(audit["invalid_rows"]), 1)

    def test_exceptional_closure_and_early_close(self):
        c = Clock(calendar_2024_2027())
        self.assertIsNone(c.session(dt.date(2025, 1, 9)))
        self.assertEqual(c.session(dt.date(2026, 11, 27))[1], dt.time(13))
        self.assertIsNone(c.session(dt.date(2023, 1, 3)))


class NumericalMetrics(unittest.TestCase):
    def test_forward_returns_exact_sessions_and_spy(self):
        m = MarketData(document())
        day = "2026-10-01"
        price = next(r["c"] for r in m.bars("A", "1d") if r["day"] == day)
        signal = dict(id="s", strategy="MINERVINI_FILTER", symbol="A", side="long", day=day,
                      at=iso(m.clock.close(day)), reference_price=price)
        rows = forward_samples(m, [signal], "2026-11-06")
        self.assertAlmostEqual(rows[0]["return_pct"], 100 / price)
        self.assertAlmostEqual(rows[1]["return_pct"], 300 / price)
        self.assertIsNotNone(rows[0]["excess_return_pct"])
        self.assertIsNone(rows[-1]["return_pct"])

    def test_short_forward_returns_and_excursions(self):
        m = MarketData(document())
        row = m.bars("A", "1d")[0]
        signal = dict(id="s", strategy="PARABOLIC_SHORT", symbol="A", side="short", day=row["day"], at=row["end"], reference_price=row["c"])
        sample = forward_samples(m, [signal])[0]
        self.assertAlmostEqual(sample["return_pct"], -1)
        self.assertLessEqual(sample["max_negative_move_pct"], -3)

    def test_missing_forward_session_not_compressed(self):
        d = document()
        first = d["bars"]["A"]["1d"][0]
        del d["bars"]["A"]["1d"][1]
        signal = dict(id="s", strategy="MINERVINI_FILTER", symbol="A", side="long", day="2026-08-03", at=first["end"], reference_price=100)
        self.assertEqual(forward_samples(MarketData(d), [signal])[0]["issue"], "MISSING_OR_DELISTED_HISTORY")

    def test_drawdown_includes_initial_equity(self):
        self.assertAlmostEqual(max_drawdown([100, 80, 120, 90]), .25)
        self.assertAlmostEqual(max_drawdown([100, 90]), .1)

    def test_distribution_and_low_sample(self):
        r = distribution([-2, 0, 5])
        self.assertEqual(r["n"], 3)
        self.assertEqual(r["mean"], 1)
        self.assertEqual(r["median"], 0)
        self.assertEqual(r["win_rate"], 1 / 3)
        self.assertEqual(r["loss_rate"], 1 / 3)
        self.assertIn("LOW_SAMPLE_CONFIDENCE", forward_metrics([], config())["1"]["flags"])

    def test_profit_factor_expectancy_r_and_loss_streak(self):
        trades = [dict(id=str(i), exit_at=str(i), pnl=p, return_pct=p, r_multiple=p / 10, holding_sessions=1, holding_minutes=5)
                  for i, p in enumerate([20, -10, -10, 30])]
        r = trade_metrics({"trades": trades})
        self.assertEqual(r["profit_factor"], 2.5)
        self.assertEqual(r["expectancy_usd"], 7.5)
        self.assertEqual(r["max_consecutive_losses"], 2)
        self.assertEqual(r["average_r"], .75)
        self.assertIsNone(trade_metrics({"trades": []})["profit_factor"])

    def test_sharpe_absent_when_methodologically_undefined(self):
        sim = {"initial_equity": 100, "daily_equity": [{"equity": 100}] * 100, "flags": [], "exposure": 0}
        r = portfolio_metrics(sim)
        self.assertIsNone(r["sharpe"])
        self.assertIsNone(r["sortino"])

    def test_spy_qqq_require_same_full_period(self):
        m = MarketData(document())
        self.assertTrue(benchmark(m, "SPY", "2026-10-01", "2026-10-09", cost_scenarios(config())["IDEAL"])["available"])
        self.assertFalse(benchmark(m, "QQQ", "2026-01-02", "2026-10-09", cost_scenarios(config())["IDEAL"])["available"])


class TradesAndSizing(unittest.TestCase):
    def test_long_short_stop_target_gaps_and_ambiguous_candle(self):
        self.assertEqual(exit_price(dict(o=100, h=121, l=95, c=115), 90, 120, "long"), (120, "TARGET"))
        self.assertEqual(exit_price(dict(o=100, h=105, l=79, c=80), 110, 80, "short"), (80, "TARGET"))
        self.assertEqual(exit_price(dict(o=85, h=100, l=84, c=99), 90, 120, "long"), (85, "GAP_STOP"))
        self.assertEqual(exit_price(dict(o=115, h=120, l=95, c=100), 110, 80, "short"), (115, "GAP_STOP"))
        self.assertEqual(exit_price(dict(o=100, h=121, l=89, c=100), 90, 120, "long"), (90, "STOP"))

    def test_spread_slippage_and_existing_cost_convention(self):
        c = {"spread_bps": 20, "slippage_bps_per_side": 5, "commission_per_unit_roundtrip": .01}
        self.assertAlmostEqual(friction(100, c), .15)
        self.assertAlmostEqual(roundtrip_cost(100, c), .31)
        scenarios = cost_scenarios(config())
        self.assertEqual(scenarios["IDEAL"]["spread_bps"], 0)
        self.assertEqual(scenarios["STRESS"]["slippage_bps_per_side"], 2 * scenarios["REALISTIC"]["slippage_bps_per_side"])

    def test_equal_weight_and_risk_sizing(self):
        c = cost_scenarios(config())["IDEAL"]
        self.assertEqual(quantity(100000, 100, 90, c, config(), "equal_weight"), 500)
        self.assertEqual(quantity(100000, 100, 90, c, config(), "fixed_risk"), 50)

    def simulate(self, prices=(100, 105, 110), side="long", costs="IDEAL", plans=None, cfg=None):
        c = cfg or config()
        p = plan(side=side, at="2026-10-01T19:40:00Z")
        return simulate(MarketData(trading_document(prices)), plans or [p], c, cost_scenarios(c)[costs], "fixed_risk")

    def test_long_pnl_r_and_time_exit(self):
        sim = self.simulate()
        trade = sim["trades"][0]
        self.assertEqual(trade["pnl"], 500)
        self.assertEqual(trade["r_multiple"], 1)
        self.assertEqual(trade["exit_reason"], "TIME_EXIT")

    def test_short_pnl_and_missing_borrow_label(self):
        sim = self.simulate((100, 95, 90), "short")
        self.assertEqual(sim["trades"][0]["pnl"], 500)
        self.assertIn("BORROW_DATA_MISSING", sim["flags"])

    def test_costs_reduce_long_and_short_pnl(self):
        for side, prices in (("long", (100, 105, 110)), ("short", (100, 95, 90))):
            ideal = self.simulate(prices, side)["trades"][0]["return_pct"]
            real = self.simulate(prices, side, "REALISTIC")["trades"][0]["return_pct"]
            stress = self.simulate(prices, side, "STRESS")["trades"][0]["return_pct"]
            self.assertGreater(ideal, real)
            self.assertGreater(real, stress)

    def test_same_bar_cannot_fill_trigger_bar_open(self):
        p = plan(at="2026-10-01T19:45:00Z")
        sim = self.simulate(plans=[p])
        self.assertEqual(sim["trades"][0]["entry"], 105)

    def test_slots_duplicates_and_ranking(self):
        a = plan(at="2026-10-01T19:40:00Z")
        b = dict(plan("B", key="b", at=a["at"]), confluence_level=3)
        c = config()
        c["max_positions"] = 1
        sim = self.simulate(plans=[a, b], cfg=c)
        self.assertEqual(sim["trades"][0]["symbol"], "B")
        self.assertIn("PORTFOLIO_SLOTS", [r["reason"] for r in sim["rejections"]])
        dup = self.simulate(plans=[a, dict(a, id="duplicate")])
        self.assertIn("DUPLICATE_TICKER", [r["reason"] for r in dup["rejections"]])

    def test_no_default_leverage(self):
        self.assertEqual(config()["leverage"], 1)
        sim = self.simulate()
        self.assertLessEqual(sim["trades"][0]["entry"] * sim["trades"][0]["qty"], config()["initial_equity"])

    def test_missing_next_slot_not_filled_from_later_bar(self):
        p = plan(at="2026-10-01T19:41:00Z")
        sim = self.simulate(plans=[p])
        self.assertFalse(sim["trades"])
        self.assertEqual(sim["rejections"][0]["reason"], "NEXT_INTRADAY_BAR_MISSING")

    def test_cfd_rejected_without_historical_contract_costs(self):
        p = dict(plan(at="2026-10-01T19:40:00Z"), instrument_type="cfd")
        sim = self.simulate(plans=[p])
        self.assertFalse(sim["trades"])
        self.assertIn("CFD_NET_PERFORMANCE_UNAVAILABLE", sim["flags"])

    def test_stop_not_known_at_signal_is_rejected(self):
        p = dict(plan(at="2026-10-01T19:40:00Z"), stop_known_at="2026-10-02T19:40:00Z")
        sim = self.simulate(plans=[p])
        self.assertEqual(sim["rejections"][0]["reason"], "STOP_FROM_FUTURE")

    def test_aggregate_risk_limit_prevents_another_full_risk_position(self):
        c = config()
        c["max_portfolio_risk"] = c["risk_per_trade"]
        a = plan(at="2026-10-01T19:40:00Z")
        b = plan("B", key="second", at=a["at"])
        sim = self.simulate(plans=[a, b], cfg=c)
        self.assertEqual(len(sim["trades"]), 1)
        self.assertEqual(sim["rejections"][0]["reason"], "CAPITAL_OR_RISK_LIMIT")

    def test_known_historical_borrow_costs_are_charged_without_guessing(self):
        d = trading_document((100, 95, 90))
        d["borrow"] = {"A": [{"day": "2026-10-01", "available_at": "2026-10-01T19:55:00Z", "cost_per_share": 1}]}
        p = plan(side="short", at="2026-10-01T19:40:00Z")
        sim = simulate(MarketData(d), [p], config(), cost_scenarios(config())["IDEAL"], "fixed_risk")
        self.assertEqual(sim["trades"][0]["pnl"], 450)
        self.assertNotIn("BORROW_DATA_MISSING", sim["flags"])
        self.assertIn("SHORT_AVAILABILITY_UNKNOWN", sim["flags"])

    def test_missing_bar_while_open_invalidates_net_result(self):
        d = trading_document()
        del d["bars"]["A"]["5m"][1]
        p = plan(at="2026-10-01T19:40:00Z")
        sim = simulate(MarketData(d), [p], config(), cost_scenarios(config())["IDEAL"], "fixed_risk")
        self.assertIn("INCOMPLETE_POSITION_HISTORY", sim["flags"])
        self.assertIsNone(portfolio_metrics(sim)["return_pct"])

    def test_initial_risk_uses_mid_distance_and_roundtrip_cost_once(self):
        sim = self.simulate(costs="REALISTIC")
        trade = sim["trades"][0]
        unit = abs(trade["entry_mid"] - trade["stop"]) + roundtrip_cost(trade["entry_mid"], sim["costs"])
        self.assertAlmostEqual(trade["planned_risk"], unit * trade["qty"])

    def test_explicit_leverage_is_separately_flagged(self):
        c = dict(config(), leverage=2)
        self.assertIn("LEVERAGED_SCENARIO", self.simulate(cfg=c)["flags"])

    def test_complete_overnight_position_has_no_false_opening_gap(self):
        d = document()
        clock = Clock(d["sessions"])
        rows = []
        for day, price in (("2026-10-01", 100), ("2026-10-02", 102)):
            start = clock.eastern_to_utc(dt.datetime.combine(dt.date.fromisoformat(day), dt.time(9, 30)))
            for i in range(78):
                begin = start + dt.timedelta(minutes=5 * i)
                finish = begin + dt.timedelta(minutes=5)
                rows.append(dict(start=iso(begin), end=iso(finish), available_at=iso(finish),
                                 o=price, c=price, h=price + 1, l=price - 1, v=100000))
        d["bars"]["A"]["5m"] = rows
        c = dict(config(), max_hold_sessions=2)
        p = plan(at="2026-10-01T19:40:00Z")
        sim = simulate(MarketData(d), [p], c, cost_scenarios(c)["IDEAL"], "fixed_risk")
        self.assertEqual(sim["flags"], [])
        self.assertEqual(sim["trades"][0]["holding_sessions"], 2)
        self.assertEqual(sim["trades"][0]["pnl"], 100)

    def test_multi_session_random_control_is_explicitly_unavailable(self):
        sim = {"trades": [dict(entry_day="2026-10-01", exit_day="2026-10-02")]}
        result = portfolio_baseline(MarketData(document()), sim, config(), {}, "fixed_risk", config()["start"], config()["end"])
        self.assertIn("MULTI_SESSION_MATCHING_UNAVAILABLE", result["flags"])


class SplitsAndBaseline(unittest.TestCase):
    def test_chronological_is_oos_walk_forward(self):
        days = [str(i).zfill(3) for i in range(200)]
        periods, windows = partitions(days, .7, 80, 40)
        self.assertEqual(len(periods["in_sample"]), 140)
        self.assertEqual(len(periods["out_of_sample"]), 60)
        self.assertEqual(len(windows), 3)
        for window in windows:
            self.assertLess(window["train"][-1], window["test"][0])

    def test_cross_boundary_returns_are_purged(self):
        rows = [dict(day="001", end_day="005", return_pct=99)]
        result = select_samples(rows, ["001", "002"])
        self.assertIsNone(result[0]["return_pct"])
        self.assertEqual(result[0]["issue"], "PURGED_SPLIT_BOUNDARY")
        self.assertFalse(select_plans([dict(day="002")], ["001", "002"], 2))

    def test_seeded_random_signal_baseline_is_reproducible(self):
        m = MarketData(document())
        first = m.bars("A", "1d")[0]
        signal = dict(id="s", strategy="MINERVINI_FILTER", symbol="A", side="long", day=first["day"], at=first["end"], reference_price=100)
        rows = forward_samples(m, [signal])
        a, b = signal_baseline(m, rows, config()), signal_baseline(m, rows, config())
        self.assertEqual(a, b)
        self.assertEqual(a["1"]["valid"], 1000)
        self.assertLess(a["1"]["p05"], a["1"]["p95"])

    def test_seeded_random_portfolio_matched_count_and_costs(self):
        m = MarketData(trading_document())
        p = plan(at="2026-10-01T19:40:00Z")
        c = config()
        costs = cost_scenarios(c)["REALISTIC"]
        sim = simulate(m, [p], c, costs, "fixed_risk")
        a = portfolio_baseline(m, sim, c, costs, "fixed_risk", c["start"], c["end"])
        b = portfolio_baseline(m, sim, c, costs, "fixed_risk", c["start"], c["end"])
        self.assertEqual(a, b)
        self.assertEqual(a["valid"], 1000)
        self.assertEqual(a["matched_trades"], len(sim["trades"]))
        self.assertAlmostEqual(a["median"], portfolio_metrics(sim)["return_pct"], places=10)

    def test_no_baseline_without_real_trades(self):
        result = portfolio_baseline(MarketData(document()), {"trades": []}, config(), {}, "fixed_risk", "2026-10-01", "2026-10-09")
        self.assertIsNone(result["median"])
        self.assertEqual(result["valid"], 0)

    def test_config_rejects_invalid_risk_and_ranking(self):
        validate(config())
        for changes in ({"max_portfolio_risk": 2}, {"ranking": ["future_return"]}, {"leverage": 0}):
            with self.assertRaises(ValueError):
                validate(dict(config(), **changes))


class AdaptersAndSourceInvariance(unittest.TestCase):
    def test_read_only_replay_does_not_create_artificial_filter_trades(self):
        c = config()
        before = fingerprints()
        with patch("urllib.request.urlopen", side_effect=AssertionError("network during replay")):
            result = replay(MarketData(document()), c)
        self.assertEqual(before, fingerprints())
        self.assertFalse(result["plans"])
        self.assertFalse(result["signals"])  # deliberately insufficient history

    def test_source_functions_and_confluence_levels_remain_original(self):
        from common import module
        cf = module("trader-confluence", "confluence")
        expected = {"CANDIDATE": "FILTER_CONFLUENCE", "SETUP_READY": "SETUP_CONFLUENCE", "TRIGGERED": "TRIGGER_CONFLUENCE"}
        for status, level in expected.items():
            self.assertEqual(cf.classify("PASS", {"setup": "BREAKOUT", "status": status})[1], level)
        self.assertIsNone(cf.classify("FAIL", {"setup": "BREAKOUT", "status": "TRIGGERED"})[1])

    def test_all_original_strategy_sources_equal_origin_main(self):
        expected = read(Path(ROOT) / "trader-backtest/strategy-sources.json")
        self.assertEqual(fingerprints(), expected["sha256"])

    def test_actual_breakout_and_confluence_trigger_replay_is_causal(self):
        # A fully synthetic contracting base plus a real strategy OR trigger.
        # Future bars and catalysts must not affect earlier output.
        from test_qullamaggie import base
        clock = Clock(calendar_2024_2027())
        days = [d for d in sorted(clock.sessions) if d <= "2026-11-06"][-300:]
        raw = base()
        bars = [dict(open=50, close=50, high=51, low=49, volume=1000000)] * (300 - len(raw)) + raw
        d = {"universe": ["FAST", "SLOW"], "sessions": clock.sessions, "bars": {}, "splits": {},
             "provenance": {"retrospective_snapshot": True}}
        for sym in d["universe"]:
            rows = []
            for i, (day, bar) in enumerate(zip(days, bars)):
                price = 50 + i * .001
                values = {k: bar[v] if sym == "FAST" else (1000000 if k == "v" else price + (1 if k == "h" else -1 if k == "l" else 0))
                          for k, v in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume"))}
                begin = clock.eastern_to_utc(dt.datetime.combine(dt.date.fromisoformat(day), dt.time(9, 30)))
                rows.append(dict(values, start=iso(begin), end=iso(clock.close(day)), available_at=iso(clock.close(day))))
            intra = []
            for i, price in enumerate((105, 105, 105, 107, 108)):
                begin = clock.eastern_to_utc(dt.datetime(2026, 11, 9, 9, 30)) + dt.timedelta(minutes=5 * i)
                finish = begin + dt.timedelta(minutes=5)
                intra.append(dict(o=price, h=price + .5, l=104.5, c=price, v=1000000,
                                  start=iso(begin), end=iso(finish), available_at=iso(finish)))
            d["bars"][sym] = {"1d": rows, "5m": intra}
        c = dict(config(), universe=d["universe"], start="2026-11-09", end="2026-11-09")
        first = replay(MarketData(d), c)
        self.assertTrue(any(p["strategy"] == "MINERVINI_ENTRY" for p in first["plans"]))
        self.assertTrue(any(s["strategy"] == "CONFLUENCE_TRIGGERED" for s in first["signals"]))
        self.assertFalse(any(p["strategy"] in ("MINERVINI_FILTER", "CONFLUENCE_CANDIDATE", "CONFLUENCE_SETUP_READY") for p in first["plans"]))
        future = dict(d["bars"]["FAST"]["1d"][-1], start="2026-11-10T14:30:00Z", end="2026-11-10T21:00:00Z",
                      available_at="2026-11-10T21:00:00Z", o=99999, h=99999, l=99999, c=99999)
        d["bars"]["FAST"]["1d"].append(future)
        self.assertEqual(first, replay(MarketData(d), c))

    def test_execution_does_not_use_future_corrected_bar(self):
        d = trading_document()
        original = d["bars"]["A"]["5m"][0]
        d["bars"]["A"]["5m"].append(dict(original, o=1000, h=1001, l=999, c=1000,
                                                 available_at="2026-11-06T22:00:00Z"))
        m = MarketData(d)
        p = plan(at="2026-10-01T19:40:00Z")
        sim = simulate(m, [p], config(), cost_scenarios(config())["IDEAL"], "fixed_risk")
        self.assertEqual(sim["trades"][0]["entry"], 100)
        self.assertIn("OUTCOMES_USE_LATEST_REPORTED_VINTAGE", m.flags)

    def test_decisions_outside_original_verified_calendar_are_rejected(self):
        c = dict(config(), start="2025-10-01", end="2025-10-02")
        with self.assertRaisesRegex(ValueError, "2026-2027"):
            replay(MarketData(document()), c)


if __name__ == "__main__":
    unittest.main()
