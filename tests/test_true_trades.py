"""Synthetic adversarial clocks and independent numerical trade/portfolio checks."""
import copy
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from _load import ROOT
sys.path.insert(0, str(Path(ROOT) / "trader-backtest"))
from common import Clock, calendar_2024_2027, iso, read, stamp
from data import MarketData
from engine import cost_scenarios
from intraday_audit import audit, describe, request_chart
from portfolio_challenge import evaluate_challenge, first_hit
from portfolio_sources import Timeline
from run import fingerprints
from true_trade_collect import freeze
from true_trade_execution import (account_trade, bar_exit, contiguous_prefix, cutoff, native_bars,
                                  reconstruct, sample_status, session_rows, trade_metrics)
from true_trade_portfolio import challenge_observations, monte_carlo, simulate, template
from true_trade_replay import QULL, catalysts_before, quality_at, replay
from true_trade_run import run

CLOCK = Clock(calendar_2024_2027())
CFG = read(Path(ROOT) / "trader-backtest/true-trade-config.json")
IDEAL = cost_scenarios(CFG)["IDEAL"]
REAL = cost_scenarios(CFG)["REALISTIC"]
OPEN = stamp("2026-11-09T14:30:00Z")


def row(i, prices=(100, 101, 99, 100), minutes=5, volume=1000, start=OPEN):
    begin = start + dt.timedelta(minutes=i * minutes)
    end = begin + dt.timedelta(minutes=minutes)
    return dict(zip(("o", "h", "l", "c"), prices), v=volume, _start=begin, _end=end,
                _available=end, start=iso(begin), end=iso(end), available_at=iso(end), interval=str(minutes) + "m")


def plan(**updates):
    return dict({"id": "BREAKOUT/DEMO/2026-11-09", "strategy": "BREAKOUT", "ticker": "DEMO", "symbol": "DEMO",
        "session": "2026-11-09", "setup_date": "2026-11-06", "setup_known_at": "2026-11-06T21:00:00Z",
        "trigger_type": "ORH_5m", "trigger_time": iso(OPEN + dt.timedelta(minutes=10)),
        "stop_initial": 95, "stop_known_at": iso(OPEN + dt.timedelta(minutes=10)), "side": "long"}, **updates)


def document(ep=False):
    days = [d for d in sorted(CLOCK.sessions) if d <= "2026-11-06"][-361:]
    prices = [50.] * 280 + [50 + i * 50 / 59 for i in range(60)] + [100 + i * .03 for i in range(20)] + [106.]
    doc = {"sessions": CLOCK.sessions, "universe": ["DEMO", "SLOW"], "bars": {}, "splits": {},
           "evidence": {"events": [], "metadata": []}, "provenance": {"retrospective_snapshot": True}}
    for sym in doc["universe"]:
        bars = []
        for i, (day, price) in enumerate(zip(days, prices)):
            p = price if sym == "DEMO" else 50.
            width = 5 - (i - 340) * 4 / 19 if sym == "DEMO" and 340 <= i < 360 else 1
            op = CLOCK.eastern_to_utc(dt.datetime.combine(dt.date.fromisoformat(day), dt.time(9, 30)))
            end = CLOCK.close(day)
            bars.append(dict(start=iso(op), end=iso(end), available_at=iso(end), o=p, h=p+width, l=p-width, c=p, v=1000000))
        doc["bars"][sym] = {"1d": bars}
    values = [(106.1,106.3,106,106.2),(106.2,107,106.1,106.8)] if not ep else [(117,117.5,116.5,117.1),(117.1,118.5,117,118)]
    values += [(106.8,107,106.5,106.8)] * 76 if not ep else [(118,118.2,117.5,118)] * 76
    doc["bars"]["DEMO"]["5m"] = [{k:v for k,v in row(i,p,volume=600000).items() if not k.startswith("_") and k != "interval"} for i,p in enumerate(values)]
    return doc


class OpeningClockTests(unittest.TestCase):
    def events(self, n, minutes=1, span=1):
        bars = [row(i, (100,101,99,100) if i < span // minutes else (100,104,99,103), minutes) for i in range(n)]
        return QULL.triggers(native_bars(bars, CLOCK), "buy", QULL.config({"opening_ranges": [span]}))

    def test_1m_range_not_available_during_first_minute(self):
        self.assertEqual(self.events(1), [])

    def test_1m_first_confirmed_break_requires_later_bar_close(self):
        self.assertEqual(self.events(2)[0]["at"], "2026-11-09T14:32:00Z")

    def test_5m_range_not_available_before_minute_five(self):
        self.assertEqual(self.events(4, span=5), [])
        self.assertEqual(self.events(5, span=5), [])

    def test_5m_orh_only_after_subsequent_close(self):
        self.assertEqual(self.events(6, span=5)[0]["at"], "2026-11-09T14:36:00Z")

    def test_60m_range_not_available_before_range_end(self):
        self.assertEqual(self.events(59, span=60), [])
        self.assertEqual(self.events(60, span=60), [])

    def test_60m_first_confirmed_break(self):
        self.assertEqual(self.events(61, span=60)[0]["at"], "2026-11-09T15:31:00Z")

    def test_5m_cannot_resolve_1m_range(self):
        self.assertEqual(self.events(10, minutes=5), [])

    def test_wick_without_close_confirmation_never_triggers(self):
        bars = native_bars([row(0), row(1, (100,200,99,100))], CLOCK)
        self.assertEqual(QULL.triggers(bars, "buy", QULL.config()), [])


class EntryExitTests(unittest.TestCase):
    def trade(self, rows=None, p=None, costs=IDEAL, order="conservative"):
        rows = rows or [row(2,(105,120,104,115))]
        return reconstruct(p or plan(), rows, CLOCK, CFG, costs, order)

    def test_entry_is_next_bar_open_not_trigger_or_best_price(self):
        result = self.trade()
        self.assertEqual(result["entry_mid"],105)
        self.assertEqual(result["entry_time"],"2026-11-09T14:40:00Z")
        self.assertNotEqual(result["entry_mid"],104)

    def test_no_fill_in_trigger_bar(self):
        result = self.trade([row(1,(90,115,80,110)),row(2,(105,130,104,120))])
        self.assertEqual(result["entry_mid"],105)

    def test_missing_next_slot_never_fills_later(self):
        self.assertEqual(self.trade([row(3,(100,120,99,119))])["status"],"NEXT_INTRADAY_BAR_MISSING")

    def test_future_stop_rejected(self):
        result=self.trade(p=plan(stop_known_at="2026-11-09T14:45:00Z"))
        self.assertEqual(result["status"],"INSUFFICIENT_STOP_DATA")

    def test_missing_stop_rejected(self):
        self.assertEqual(self.trade(p=plan(stop_initial=None))["status"],"INSUFFICIENT_STOP_DATA")

    def test_missing_stop_availability_not_inferred(self):
        p=plan();del p["stop_known_at"]
        self.assertEqual(self.trade(p=p)["status"],"INSUFFICIENT_STOP_DATA")

    def test_zero_volume_open_not_asserted_tradeable(self):
        self.assertEqual(self.trade([row(2,volume=0)])["status"],"INSUFFICIENT_EXECUTION_LIQUIDITY")

    def test_daily_setup_unavailable_until_close(self):
        self.assertEqual(self.trade(p=plan(setup_known_at="2026-11-09T21:00:00Z"))["status"],"SETUP_NOT_AVAILABLE_AT_TRIGGER")

    def test_entry_gap_beyond_stop_rejected(self):
        self.assertEqual(self.trade([row(2,(94,100,90,99))])["status"],"ENTRY_OUTSIDE_VALID_STOP")

    def test_stop_before_later_target(self):
        result=self.trade([row(2,(100,101,94,100)),row(3,(100,130,99,125))])
        self.assertEqual((result["exit_reason"],result["exit_mid"]),("STOP_EXIT",95))

    def test_target_before_later_stop(self):
        result=self.trade([row(2,(100,111,99,110)),row(3,(100,101,90,95))])
        self.assertEqual((result["exit_reason"],result["exit_mid"]),("TARGET_EXIT",110))

    def test_both_same_bar_conservative(self):
        result=self.trade([row(2,(100,112,94,100))])
        self.assertEqual(result["exit_mid"],95)
        self.assertIn("AMBIGUOUS_INTRABAR_ORDER",result["ambiguity_flags"])

    def test_both_same_bar_optimistic_sensitivity(self):
        self.assertEqual(self.trade([row(2,(100,112,94,100))],order="optimistic")["exit_mid"],110)

    def test_gap_stop_uses_adverse_open(self):
        self.assertEqual(bar_exit(dict(o=90,h=100,l=89,c=99),95,110,"long")[:2],(90,"STOP_EXIT"))

    def test_open_target_known_before_later_stop(self):
        self.assertEqual(bar_exit(dict(o=115,h=120,l=90,c=100),95,110,"long")[:2],(110,"TARGET_EXIT"))

    def test_short_both_same_bar(self):
        price,reason,flags,_=bar_exit(dict(o=100,h=106,l=89,c=100),105,90,"short")
        self.assertEqual((price,reason),(105,"STOP_EXIT"))
        self.assertIn("AMBIGUOUS_INTRABAR_ORDER",flags)

    def test_time_exit_uses_scheduled_open_not_later_wick(self):
        rows=[row(i,(100,101,99,100)) for i in range(2,78)]
        rows[77-2].update(o=102,h=150,c=140)
        result=self.trade(rows)
        self.assertEqual(result["exit_reason"],"TIME_EXIT")
        self.assertEqual(result["exit_mid"],102)
        self.assertEqual(result["exit_time"],"2026-11-09T20:55:00Z")

    def test_intrabar_exit_time_is_interval(self):
        result=self.trade([row(2,(100,111,99,110))])
        self.assertEqual(result["exit_time_lower_bound"],"2026-11-09T14:40:00Z")
        self.assertEqual(result["exit_time_upper_bound"],"2026-11-09T14:45:00Z")

    def test_data_end_is_not_closed_trade(self):
        result=self.trade([row(2)])
        self.assertEqual(result["exit_reason"],"DATA_END")
        self.assertFalse(result["true_trade"])

    def test_exit_gap_not_skipped(self):
        result=self.trade([row(2),row(4,(100,120,99,115))])
        self.assertEqual(result["exit_reason"],"INSUFFICIENT_EXIT_DATA")
        self.assertNotIn("net_pnl",result)

    def test_exit_zero_volume_does_not_prove_a_fill(self):
        result=self.trade([row(2),row(3,(100,130,90,100),volume=0)])
        self.assertEqual(result["exit_reason"],"INSUFFICIENT_EXIT_DATA")

    def test_entry_after_flat_by_refused(self):
        p=plan(trigger_time="2026-11-09T20:55:00Z")
        self.assertEqual(self.trade([row(77)],p)["status"],"AFTER_FLAT_BY")

    def test_early_close_uses_existing_five_minute_buffer(self):
        self.assertEqual(iso(cutoff(CLOCK,"2026-11-27",CFG)),"2026-11-27T17:55:00Z")


class AccountingTests(unittest.TestCase):
    def test_long_pnl(self):
        self.assertEqual(account_trade(100,110,95,"long",IDEAL)["net_pnl"],10)

    def test_short_pnl(self):
        self.assertEqual(account_trade(100,90,105,"short",IDEAL)["net_pnl"],10)

    def test_losing_short(self):
        self.assertEqual(account_trade(100,110,105,"short",IDEAL)["net_pnl"],-10)

    def test_spread_separately_at_both_fills(self):
        costs=dict(IDEAL,spread_bps=10)
        result=account_trade(100,110,95,"long",costs)
        self.assertAlmostEqual(result["spread"],.105)
        self.assertAlmostEqual(result["entry_price"],100.05)
        self.assertAlmostEqual(result["exit_price"],109.945)

    def test_slippage_per_side(self):
        result=account_trade(100,110,95,"long",dict(IDEAL,slippage_bps_per_side=5))
        self.assertAlmostEqual(result["slippage"],.105)

    def test_short_friction_always_adverse(self):
        result=account_trade(100,90,105,"short",REAL)
        self.assertLess(result["entry_price"],100)
        self.assertGreater(result["exit_price"],90)
        self.assertLess(result["net_pnl"],10)

    def test_fees_not_double_counted(self):
        result=account_trade(100,110,95,"long",REAL)
        self.assertAlmostEqual(result["net_pnl"],result["exit_price"]-result["entry_price"]-.01)
        self.assertAlmostEqual(result["gross_pnl"]-result["net_pnl"],result["costs"])

    def test_r_uses_actual_stop_and_explicit_cost_convention(self):
        result=account_trade(100,110,95,"long",REAL)
        self.assertAlmostEqual(result["r_multiple"],9.78/5.205)
        self.assertEqual(result["gross_r_multiple"],2)

    def test_zero_risk_is_rejected(self):
        with self.assertRaises(ValueError):account_trade(100,110,100,"long",IDEAL)

    def test_stress_costs_worse_than_realistic(self):
        costs=cost_scenarios(CFG)
        self.assertLess(account_trade(100,110,95,"long",costs["STRESS"])["net_pnl"],account_trade(100,110,95,"long",REAL)["net_pnl"])

    def test_sample_boundaries(self):
        for n,expected in [(0,"VERY_LOW_SAMPLE"),(29,"VERY_LOW_SAMPLE"),(30,"LOW_SAMPLE"),(99,"LOW_SAMPLE"),(100,"EXPLORATORY_SAMPLE")]:
            with self.subTest(n=n):self.assertEqual(sample_status(n),expected)

    def test_incomplete_trades_do_not_enter_metrics(self):
        self.assertEqual(trade_metrics([dict(true_trade=False,net_pnl=9999)])["trades"],0)
        self.assertIsNone(trade_metrics([])["expectancy"])

    def test_metrics_independently_calculated(self):
        rows=[dict(true_trade=True,id=str(i),exit_time=str(i),net_pnl=p,r_multiple=p/5,holding_minutes=10+i) for i,p in enumerate([2,-1,-1,3])]
        result=trade_metrics(rows)
        expected={"trades":4,"win_rate":.5,"average_winner":2.5,"average_loser":-1,
            "median_trade":.5,"expectancy":.75,"profit_factor":2.5,"mean_r":.15,
            "median_r":.1,"max_consecutive_losses":2,"max_consecutive_wins":1,"average_holding_minutes":11.5}
        for key,value in expected.items():
            with self.subTest(key=key):self.assertAlmostEqual(result[key],value)


class NativeReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc=document()
        cls.market=MarketData(cls.doc)
        cls.adapted=replay(cls.market,CFG,"2026-11-09","2026-11-09")

    def test_native_breakout_produces_reconstructible_plan(self):
        plans=[p for p in self.adapted["plans"] if p["strategy"]=="BREAKOUT"]
        self.assertEqual(len(plans),1)
        self.assertEqual(plans[0]["trigger_time"],"2026-11-09T14:40:00Z")

    def test_session_low_frozen_at_trigger_not_later_low(self):
        original=next(p for p in self.adapted["plans"] if p["strategy"]=="BREAKOUT")
        doc=copy.deepcopy(self.doc)
        doc["bars"]["DEMO"]["5m"][2].update(l=1)
        changed=replay(MarketData(doc),CFG,"2026-11-09","2026-11-09")
        later=next(p for p in changed["plans"] if p["strategy"]=="BREAKOUT")
        self.assertEqual(original["stop_initial"],106)
        self.assertEqual(later["stop_initial"],106)

    def test_session_high_short_only_uses_trigger_prefix(self):
        bars=native_bars([row(0,(100,105,99,100)),row(1,(100,103,96,97)),row(2,(100,999,95,96))],CLOCK)
        event=QULL.triggers(bars[:2],"sell",QULL.config())[0]
        native={"setup_status":"SETUP_READY","variant":"PARABOLIC_SHORT","setup":"qullamaggie-parabolic","reasons":[]}
        QULL.apply_trigger(native,[event],bars[:2],[],QULL.config())
        self.assertEqual(native["stop"]["price"],105)

    def test_parabolic_long_first_green_is_independent_of_short_extension(self):
        bars=native_bars([row(0,(100,101,98,100.5)),row(1,(100.5,1000,1,100))],CLOCK)
        event=next(e for e in QULL.triggers(bars,"buy",QULL.config(),True) if e["type"]=="FIRST_GREEN_5m")
        native={"setup_status":"SETUP_READY","variant":"PARABOLIC_LONG","setup":"qullamaggie-parabolic","reasons":[]}
        QULL.apply_trigger(native,[event],bars[:1],[],QULL.config())
        self.assertEqual(native["stop"]["price"],98)
        self.assertEqual(native["trigger"]["at"],"2026-11-09T14:35:00Z")

    def test_future_data_gap_does_not_erase_earlier_trigger(self):
        doc=copy.deepcopy(self.doc)
        del doc["bars"]["DEMO"]["5m"][4]
        adapted=replay(MarketData(doc),CFG,"2026-11-09","2026-11-09")
        self.assertTrue(any(p["strategy"]=="BREAKOUT" for p in adapted["plans"]))

    def test_confluence_requires_real_breakout_parent(self):
        parents={p["id"] for p in self.adapted["plans"] if p["strategy"]=="BREAKOUT"}
        con=[p for p in self.adapted["plans"] if p["strategy"]=="CONFLUENCE_TRIGGERED"]
        self.assertTrue(con)
        for p in con:
            self.assertEqual(p["minervini_status"],"PASS")
            self.assertIn(p["parent_breakout_id"],parents)

    def test_no_daily_fallback(self):
        doc=copy.deepcopy(self.doc)
        del doc["bars"]["DEMO"]["5m"]
        adapted=replay(MarketData(doc),CFG,"2026-11-09","2026-11-09")
        self.assertEqual(adapted["plans"],[])

    def test_no_minervini_entry_generated(self):
        self.assertFalse(any("MINERVINI" in p["strategy"] for p in self.adapted["plans"]))

    def test_pit_safe_is_causal_and_snapshot_not_certified(self):
        q=quality_at(self.market,"DEMO",OPEN)
        self.assertEqual(q["pit_safe_status"],"PIT_SAFE")
        self.assertEqual(q["data_quality_status"],"NOT_HISTORICALLY_PIT_CERTIFIED")
        doc=copy.deepcopy(self.doc)
        future=dict(doc["bars"]["DEMO"]["1d"][-1],start="2026-11-09T14:30:00Z",end="2026-11-09T21:00:00Z",available_at="2026-11-09T21:00:00Z",o=1,c=1,h=2,l=.1)
        doc["bars"]["DEMO"]["1d"].append(future)
        self.assertEqual(q,quality_at(MarketData(doc),"DEMO",OPEN))

    def test_future_metadata_never_filters_pit_admission(self):
        doc=copy.deepcopy(self.doc)
        doc["provenance"]["current_identity"]={"instrumentType":"ETF","currency":"EUR"}
        self.assertEqual(quality_at(self.market,"DEMO",OPEN),quality_at(MarketData(doc),"DEMO",OPEN))

    def test_vwap_event_invariant_to_future_price_volume(self):
        bars=native_bars([row(i,p) for i,p in enumerate([(100,102,99,101),(101,101,95,96),(96,101,95,99),(99,100,94,95)])],CLOCK)
        events=QULL.triggers(bars,"sell",QULL.config(),True)
        vwap=next(e for e in events if e["type"]=="VWAP_FAILURE")
        reference=sum((b["high"]+b["low"]+b["close"])/3*b["volume"] for b in bars)/sum(b["volume"] for b in bars)
        self.assertAlmostEqual(vwap["level"],reference)
        more=bars+native_bars([row(4,(100,10000,99,1000),volume=999999999)],CLOCK)
        self.assertEqual(vwap,next(e for e in QULL.triggers(more,"sell",QULL.config(),True) if e["type"]=="VWAP_FAILURE"))

    def test_missing_catalyst_prevents_ep(self):
        adapted=replay(MarketData(document(True)),CFG,"2026-11-09","2026-11-09")
        ep=next(r for r in adapted["audit"] if r["strategy"]=="EP" and r["ticker"]=="DEMO")
        self.assertEqual(ep["reason"],"INSUFFICIENT_CATALYST_DATA")
        self.assertFalse(any(p["strategy"]=="EP" for p in adapted["plans"]))

    def test_future_volume_does_not_confirm_earlier_ep_trigger(self):
        doc=document(True)
        for b in doc["bars"]["DEMO"]["5m"][:2]:b["v"]=1
        doc["evidence"]["events"]=[dict(symbol="DEMO",source_skill="trader-news",category="earnings",confirmed=True,reference="synthetic:test",published_at="2026-11-09T14:00:00Z",available_at="2026-11-09T14:00:00Z")]
        adapted=replay(MarketData(doc),CFG,"2026-11-09","2026-11-09")
        self.assertFalse(any(p["strategy"]=="EP" for p in adapted["plans"]))

    def test_ep_catalyst_must_precede_trigger_and_entry(self):
        doc=document(True)
        event=dict(symbol="DEMO",source_skill="trader-news",category="earnings",confirmed=True,reference="synthetic:test",published_at="2026-11-09T14:39:00Z",available_at="2026-11-09T14:39:00Z")
        doc["evidence"]["events"]=[event]
        adapted=replay(MarketData(doc),CFG,"2026-11-09","2026-11-09")
        self.assertTrue(any(p["strategy"]=="EP" for p in adapted["plans"]))
        event.update(published_at="2026-11-09T14:41:00Z",available_at="2026-11-09T14:41:00Z")
        adapted=replay(MarketData(doc),CFG,"2026-11-09","2026-11-09")
        self.assertFalse(any(p["strategy"]=="EP" for p in adapted["plans"]))

    def test_same_timestamp_catalyst_is_not_before_trigger(self):
        doc=document(True)
        doc["evidence"]["events"]=[dict(symbol="DEMO",source_skill="trader-news",category="earnings",confirmed=True,reference="synthetic:test",published_at="2026-11-09T14:40:00Z",available_at="2026-11-09T14:40:00Z")]
        self.assertEqual(catalysts_before(MarketData(doc),"DEMO",stamp("2026-11-09T14:40:00Z"))[1],[])

    def test_native_strategies_and_config_unchanged(self):
        expected=read(Path(ROOT)/"trader-backtest/strategy-sources.json")
        self.assertEqual(fingerprints(),expected["sha256"])
        pilot=read(Path(ROOT)/"trader-backtest/config.first.json")
        for key in ("target_r","flat_by","max_hold_sessions","costs","universe"):
            self.assertEqual(CFG[key],pilot[key])

    def test_complete_offline_run_with_sensitivity_and_real_ledger(self):
        config=dict(CFG,universe=["DEMO","SLOW"])
        with tempfile.TemporaryDirectory() as folder:
            result=run(self.doc,config,folder)
            self.assertEqual(len(result["sensitivities"]),6)
            self.assertEqual(result["strategies"]["BREAKOUT"]["metrics"]["trades"],1)
            self.assertTrue((Path(folder)/"true-trades.csv.gz").exists())
            self.assertTrue(result["strategy_sources_unchanged"])


class PortfolioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.market=MarketData(document())
        cls.timeline=Timeline(cls.market,"2026-11-09","2026-11-09")

    def pool(self,n=1,side="long"):
        return [dict(id=str(i),symbol="T"+str(i),side=side,entry_t=10,exit_t=20,
            entry_mid=100,exit_mid=110 if side=="long" else 90,stop=95 if side=="long" else 105,target=110 if side=="long" else 90,
            marks=[[10,100,1],[15,105,1],[20,110 if side=="long" else 90,1]]) for i in range(n)]

    def test_position_size_from_actual_stop_distance(self):
        book=simulate(self.pool(),self.timeline,CFG,IDEAL,.005)
        self.assertEqual(book["trades"][0]["qty"],100)

    def test_max_five_positions_and_no_implicit_leverage(self):
        book=simulate(self.pool(8),self.timeline,CFG,REAL,.01)
        for a in book["admissions"]:
            self.assertLessEqual(a["positions"],5)
            self.assertGreaterEqual(a["cash"],0)
            self.assertLessEqual(a["exposure"],1+1e-12)
            self.assertLessEqual(a["stop_risk"],a["equity"]*.03+1e-9)

    def test_exact_five_position_limit_before_risk_cap(self):
        book=simulate(self.pool(8),self.timeline,CFG,IDEAL,.0025)
        self.assertEqual(len(book["admissions"]),5)
        self.assertEqual(sum(r["reason"]=="PORTFOLIO_SLOTS" for r in book["rejections"]),3)

    def test_cash_caps_tight_stop_position(self):
        p=self.pool()[0];p.update(stop=99.99)
        book=simulate([p],self.timeline,CFG,REAL,.01)
        self.assertLessEqual(book["admissions"][0]["exposure"],1)

    def test_duplicate_ticker_not_admitted(self):
        pool=self.pool(2);pool[1]["symbol"]=pool[0]["symbol"]
        book=simulate(pool,self.timeline,CFG,IDEAL,.005)
        self.assertEqual(len(book["trades"]),1)
        self.assertEqual(book["rejections"][0]["reason"],"DUPLICATE_TICKER")

    def test_unknown_short_borrow_not_invented(self):
        book=simulate(self.pool(side="short"),self.timeline,CFG,IDEAL,.005)
        self.assertEqual(book["rejections"][0]["reason"],"HISTORICAL_BORROW_UNAVAILABLE")

    def test_known_short_accounting_and_collateral(self):
        pool=self.pool(side="short");pool[0].update(borrow_verified=True,borrow_cost=.1)
        book=simulate(pool,self.timeline,CFG,IDEAL,.005)
        self.assertAlmostEqual(book["end_equity"],100990)
        self.assertAlmostEqual(book["trades"][0]["pnl"],990)

    def test_monte_carlo_blocked_below_100(self):
        with patch("true_trade_portfolio.simulate") as engine:
            for n in (0,29,30,99):
                self.assertEqual(monte_carlo(self.pool(n),self.timeline,CFG,REAL,.005,"TRADE_IID")["completed_paths"],0)
            engine.assert_not_called()

    def test_monte_carlo_requires_10000_paths(self):
        with self.assertRaises(ValueError):monte_carlo(self.pool(100),self.timeline,dict(CFG,paths=100),REAL,.005,"TRADE_IID")

    def test_block_bootstrap_requires_two_time_blocks(self):
        result=monte_carlo(self.pool(100),self.timeline,CFG,REAL,.005,"SESSION_BLOCK")
        self.assertEqual(result["status"],"INSUFFICIENT_TIME_BLOCKS")

    def test_10000_iid_paths_reapply_risk_and_report_requested_statistics(self):
        pool=self.pool(100)
        for i,p in enumerate(pool):
            p.update(entry_t=3*i+1,exit_t=3*i+2,marks=[[3*i+1,100,1],[3*i+2,110,1]])
        result=monte_carlo(pool,self.timeline,CFG,IDEAL,.005,"TRADE_IID")
        equity=100000
        for _ in range(100):equity+=10*int(equity*.005/5)
        self.assertEqual(result["completed_paths"],10000)
        self.assertEqual(result["end_capital_quantiles"]["P05"],equity)
        self.assertEqual(result["end_capital_quantiles"]["P95"],equity)
        self.assertEqual(result["probabilities"]["negative_total_performance"],0)
        self.assertEqual(result["drawdown_quantiles"]["P95"],0)
        self.assertEqual(result["sequence_risk"]["worst_simulated_loss_streak"],0)

    def test_challenge_no_hit_guard_and_adapter(self):
        book=simulate(self.pool(),self.timeline,CFG,IDEAL,.005)
        obs=challenge_observations(book,self.timeline)
        result=evaluate_challenge(obs,100000,{"profit_target":.5,"maximum_total_loss":.2})
        self.assertEqual(result["status"],"NO_HIT")
        self.assertIsNone(first_hit([False]*10))

    def test_challenge_adapter_preserves_risk_at_entry(self):
        book=simulate(self.pool(),self.timeline,CFG,IDEAL,.005)
        result=evaluate_challenge(challenge_observations(book,self.timeline),100000,{"maximum_risk_per_trade":.0025})
        self.assertEqual(result["status"],"LOSS_FIRST")


class AcquisitionTests(unittest.TestCase):
    def test_rate_limit_stops_audit_without_rotation(self):
        with tempfile.TemporaryDirectory() as folder, patch("intraday_audit.request_chart",return_value=({"http_status":429,"retry_after":"60"},b'{}')) as request:
            result=audit(folder)
            self.assertEqual(len(result["yahoo"]["probes"]),1)
            request.assert_called_once()

    def test_audit_reports_granularity_and_extended_hours(self):
        payload={"chart":{"result":[{"meta":{"dataGranularity":"5m","exchangeTimezoneName":"America/New_York"},"timestamp":[1794229200,1794258000]}]}}
        result=describe(json.dumps(payload).encode(),"5m")
        self.assertEqual(result["granularity"],"5m")
        self.assertTrue(result["monotonic_timestamps"])

    def test_provider_limit_error_preserved(self):
        payload={"chart":{"result":None,"error":{"description":"last 60 days"}}}
        result=describe(json.dumps(payload).encode(),"5m")
        self.assertEqual(result["status"],"UNAVAILABLE")
        self.assertIn("60 days",result["provider_error"]["description"])

    def test_frozen_protocol_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"frozen.json";freeze(path,{"a":1})
            with self.assertRaises(ValueError):freeze(path,{"a":2})

    def test_delayed_bar_revision_never_replaces_execution(self):
        doc=document();bar=doc["bars"]["DEMO"]["5m"][1]
        revised=dict(bar,c=150,h=151,available_at="2026-11-09T22:00:00Z")
        doc["bars"]["DEMO"]["5m"].append(revised)
        _,rows=session_rows(MarketData(doc),"DEMO","2026-11-09")
        self.assertEqual(rows[1]["c"],106.8)

    def test_incomplete_opening_grid_never_backfills(self):
        self.assertEqual(contiguous_prefix([row(1),row(2)],OPEN),[])

    def test_choose_5m_when_1m_starts_late_not_by_future_outcome(self):
        doc=document();doc["bars"]["DEMO"]["1m"]=[{k:v for k,v in row(10,minutes=1).items() if not k.startswith("_") and k!="interval"}]
        self.assertEqual(session_rows(MarketData(doc),"DEMO","2026-11-09")[0],"5m")


if __name__=="__main__":unittest.main()
