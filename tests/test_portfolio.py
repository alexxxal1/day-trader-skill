"""Synthetic portfolio fixtures; no network and no claims about real trades."""
import copy
import datetime as dt
import hashlib
from pathlib import Path
import random
import sys
import tempfile
import unittest

import numpy as np

from _load import ROOT
sys.path.insert(0, str(Path(ROOT) / "trader-backtest"))
from common import read
from engine import cost_scenarios, exit_price
from portfolio_challenge import evaluate_challenge, first_hit
from portfolio_engine import simulate
from portfolio_metrics import drawdowns, metrics, streaks
from portfolio_model import FX, position_size, unit_values, validate
from portfolio_monte_carlo import block_sample, bootstrap_indices, monte_carlo, quantiles, summarize, true_trade_sample_status


def configuration():
    c = read(Path(ROOT) / "trader-backtest/portfolio-config.json")
    c.update(account_currency="USD", paths=100, block_sessions=2)
    c["risk_rules"] = {"cash_buffer_pct": 0, "max_position_pct": 100, "max_order_usd": 1e9,
        "min_order_usd": 0, "min_reward_risk": 0, "max_daily_loss_usd": 1e9,
        "max_trades_per_day": 100, "max_orders_per_day": 100}
    return c


class TimelineFixture:
    def __init__(self, n=8):
        self.days = [(dt.date(2026, 1, 5) + dt.timedelta(days=i)).isoformat() for i in range(n)]
        self.opens = [dt.datetime.fromisoformat(d + "T14:30:00") for d in self.days]
        self.closes = [v + dt.timedelta(minutes=390) for v in self.opens]
        self.starts = [390 * i for i in range(n)]
        self.ends = [t + 390 for t in self.starts]
        self.total = n * 390


IDEAL = {"spread_bps":0,"slippage_bps_per_side":0,"commission_per_unit_roundtrip":0}
REAL = {"spread_bps":10,"slippage_bps_per_side":5,"commission_per_unit_roundtrip":0.01}


def trade(symbol="AAA", entry=20, end=80, exit_mid=110, side="long", **extra):
    return {"id":symbol + "/" + str(entry),"symbol":symbol,"strategy":"MINERVINI","side":side,
        "entry_t":entry,"exit_t":end,"entry_mid":100,"exit_mid":exit_mid,"entry_fx":1,"exit_fx":1,
        "stop":95 if side=="long" else 105,"target":120 if side=="long" else 80,
        "marks":[[entry+10,100,1],[end,exit_mid,1]],"trigger_setup_status":3,"RS_PROXY":90,"liquidity":1e6,
        **extra}


class PositionSizing(unittest.TestCase):
    def test_requested_three_risks(self):
        c=configuration()
        for risk,qty in ((.0025,5),(.005,10),(.01,20)):
            q, u=position_size(10000,10000,0,100,95,120,"long",1,IDEAL,c,risk)
            self.assertEqual(q,qty)
            self.assertLessEqual(q*u["risk"],10000*risk)

    def test_spread_slippage_and_roundtrip_commission(self):
        u=unit_values(100,95,120,"long",1,REAL)
        self.assertAlmostEqual(u["entry"],100.1)
        self.assertAlmostEqual(u["stop_fill"],94.905)
        self.assertAlmostEqual(u["risk"],5.205)
        q,_=position_size(10000,10000,0,100,95,120,"long",1,REAL,configuration(),.005)
        self.assertEqual(q,9)

    def test_invalid_stops_directions_and_nonfinite(self):
        for stop in (100,101,0,float("nan")):
            with self.assertRaises(ValueError):
                unit_values(100,stop,120,"long",1,IDEAL)
        with self.assertRaises(ValueError):
            unit_values(100,95,80,"short",1,IDEAL)

    def test_cash_and_portfolio_risk_caps(self):
        c=configuration()
        q,u=position_size(10000,123,299,100,95,120,"long",1,IDEAL,c,.01)
        self.assertEqual(q,0)
        q,u=position_size(10000,123,0,100,95,120,"long",1,IDEAL,c,.01)
        self.assertEqual(q,1)
        self.assertLessEqual(q*u["cash"],123)

    def test_fx_risk_and_quantity(self):
        q,u=position_size(10000,10000,0,100,95,120,"long",2,IDEAL,configuration(),.005)
        self.assertEqual(q,20)
        self.assertEqual(u["risk"],2.5)

    def test_cost_scenarios_exact_existing_convention(self):
        c=read(Path(ROOT)/"trader-backtest/config.first.json")
        scenarios=cost_scenarios(c)
        self.assertEqual(scenarios["IDEAL"],IDEAL)
        self.assertEqual(scenarios["STRESS"]["slippage_bps_per_side"],10)
        self.assertEqual(scenarios["STRESS"]["spread_bps"],20)

    def test_invalid_config_and_fixed_ranking(self):
        for key,value in (("paths",0),("risk_per_trade",[float("nan")]),("leverage",2),("ranking",[])):
            c=configuration();c[key]=value
            with self.assertRaises(ValueError):
                validate(c)


class PortfolioBook(unittest.TestCase):
    def test_long_pnl_equity_and_r(self):
        b=simulate([trade()],TimelineFixture(),configuration(),IDEAL,.005)
        self.assertEqual(b["trades"][0]["qty"],10)
        self.assertEqual(b["trades"][0]["pnl"],100)
        self.assertEqual(b["trades"][0]["r_multiple"],2)
        self.assertEqual(b["end_equity"],10100)
        self.assertEqual(b["curve"][0]["equity"],10000)
        self.assertEqual(b["daily_equity"][0]["equity"],10100)
        self.assertEqual(b["max_positions"],1)
        self.assertAlmostEqual(b["average_positions"],60/(8*390))

    def test_short_pnl_requires_explicit_borrow_and_permission(self):
        c=configuration();c["risk_rules"].update(allow_short=True,allow_margin=True)
        t=trade(side="short",exit_mid=90,borrow_verified=True,borrow_cost_usd_roundtrip=0)
        b=simulate([t],TimelineFixture(),c,IDEAL,.005)
        self.assertEqual(b["trades"][0]["pnl"],100)
        self.assertEqual(b["end_equity"],10100)
        t.pop("borrow_verified")
        self.assertEqual(simulate([t],TimelineFixture(),c,IDEAL,.005)["rejections"][0]["reason"],"HISTORICAL_BORROW_UNAVAILABLE")

    def test_net_long_pnl_costs_are_charged_once(self):
        b=simulate([trade()],TimelineFixture(),configuration(),REAL,.005)
        t=b["trades"][0]
        self.assertEqual(t["qty"],9)
        self.assertAlmostEqual(t["pnl"],9*(109.89-100.1-.01))
        self.assertAlmostEqual(t["costs"],9*(.1+.11+.01))
        self.assertAlmostEqual(b["end_equity"],10000+t["pnl"])

    def test_short_costs_and_observed_borrow_are_charged_once(self):
        c=configuration();c["risk_rules"].update(allow_short=True,allow_margin=True)
        t=trade(side="short",exit_mid=90,borrow_verified=True,borrow_cost_usd_roundtrip=.25)
        b=simulate([t],TimelineFixture(),c,REAL,.005)
        self.assertEqual(b["trades"][0]["qty"],9)
        self.assertAlmostEqual(b["trades"][0]["pnl"],9*(99.9-90.09-.01-.25))
        self.assertAlmostEqual(b["end_equity"],10000+b["trades"][0]["pnl"])

    def test_portfolio_slots_and_predeclared_ranking(self):
        ts=[trade(chr(65+i),RS_PROXY=i) for i in range(7)]
        b=simulate(ts,TimelineFixture(),configuration(),IDEAL,.0025)
        self.assertEqual(b["max_positions"],5)
        self.assertEqual(len(b["trades"]),5)
        self.assertEqual(b["admissions"][0]["id"],"G/20")
        self.assertEqual([r["reason"] for r in b["rejections"]],["PORTFOLIO_SLOTS"]*2)

    def test_total_risk_admission_and_no_capital_overdraft(self):
        c=configuration();c["max_positions"]=10
        b=simulate([trade(chr(65+i)) for i in range(10)],TimelineFixture(),c,REAL,.01)
        for a in b["admissions"]:
            self.assertLessEqual(a["used_risk"],a["equity"]*.03+1e-8)
            self.assertGreaterEqual(a["cash"],0)
        self.assertLessEqual(b["max_positions"],4)

    def test_no_duplicate_ticker(self):
        b=simulate([trade(),trade(entry=25,end=85)],TimelineFixture(),configuration(),IDEAL,.0025)
        self.assertEqual(len(b["trades"]),1)
        self.assertEqual(b["rejections"][0]["reason"],"DUPLICATE_TICKER")

    def test_fx_changes_real_long_capital(self):
        t=trade(exit_mid=100,exit_fx=2,marks=[[80,100,2]])
        b=simulate([t],TimelineFixture(),configuration(),IDEAL,.005)
        self.assertEqual(b["trades"][0]["pnl"],-500)
        self.assertEqual(b["end_equity"],9500)

    def test_daily_limit_and_window(self):
        c=configuration();c["risk_rules"]["max_daily_loss_usd"]=25
        b=simulate([trade(),trade("BBB")],TimelineFixture(),c,IDEAL,.01)
        self.assertEqual(b["trades"][0]["planned_risk"],25)
        self.assertEqual(len(b["trades"]),1)
        b=simulate([trade(entry=1)],TimelineFixture(),c,IDEAL,.0025)
        self.assertEqual(b["rejections"][0]["reason"],"ENTRY_WINDOW")

    def test_gaps_and_same_bar_stop_target_are_conservative(self):
        self.assertEqual(exit_price({"o":90,"h":125,"l":85},95,120,"long"),(90,"GAP_STOP"))
        self.assertEqual(exit_price({"o":100,"h":125,"l":90},95,120,"long"),(95,"STOP"))
        self.assertEqual(exit_price({"o":110,"h":115,"l":75},105,80,"short"),(110,"GAP_STOP"))

    def test_open_path_marked_not_fictitiously_closed(self):
        tl=TimelineFixture(1)
        t=trade(end=500,marks=[[300,90,1],[500,110,1]])
        b=simulate([t],tl,configuration(),IDEAL,.005)
        self.assertEqual(b["open_positions"],1)
        self.assertFalse(b["trades"])
        self.assertEqual(b["end_equity"],9900)

    def test_simultaneous_cross_sectional_marks_do_not_create_phantom_drawdown(self):
        a=trade("A",stop=80,target=150,marks=[[50,120,1],[80,110,1]])
        b=trade("B",stop=80,target=150,exit_mid=90,marks=[[50,80,1],[80,90,1]])
        book=simulate([a,b],TimelineFixture(),configuration(),IDEAL,.005)
        self.assertEqual(drawdowns([r["equity"] for r in book["curve"]])["maximum"],0)

    def test_exit_uses_actual_exit_mid_without_fictitious_close_price_peak(self):
        p=trade(exit_mid=95,marks=[[80,140,1]])
        b=simulate([p],TimelineFixture(),configuration(),IDEAL,.005)
        self.assertEqual(b["end_equity"],9950)
        self.assertAlmostEqual(drawdowns([r["equity"] for r in b["curve"]])["maximum"],.005)


class Statistics(unittest.TestCase):
    def test_drawdowns_duration_and_average(self):
        d=drawdowns([100,110,99,88,110,100])
        self.assertAlmostEqual(d["maximum"],.2)
        self.assertEqual(d["longest_observations"],2)
        self.assertAlmostEqual(d["average"],(.1+.2+1-100/110)/6)

    def test_consecutive_wins_losses_and_zero_break(self):
        self.assertEqual(streaks([-1,-1,0,2,2,2,-1]),(3,2))

    def test_profit_factor_expectancy_and_median(self):
        tl=TimelineFixture()
        b=simulate([trade(entry=20,end=80,exit_mid=110),trade("BBB",entry=100,end=180,exit_mid=95)],tl,configuration(),IDEAL,.005)
        m=metrics(b,tl)
        self.assertEqual(m["profit_factor"],2)
        self.assertEqual(m["expectancy"],25)
        self.assertEqual(m["median_trade"],25)
        self.assertEqual(m["average_r"],.5)
        self.assertIsNone(m["sharpe"])

    def test_undefined_profit_factor_and_ratios(self):
        b=simulate([trade()],TimelineFixture(),configuration(),IDEAL,.005)
        m=metrics(b,TimelineFixture())
        self.assertIsNone(m["profit_factor"])
        self.assertIsNone(m["sortino"])

    def test_quantiles_and_probabilities(self):
        self.assertEqual(quantiles([0,100])["P25"],25)
        s=summarize([75,100,125,200],[.01,.1,.21,.31],[0]*4,[1,2,3,4],100)
        self.assertEqual(s["probabilities"]["negative_total_performance"],.25)
        self.assertEqual(s["probabilities"]["gain_ge_25_pct"],.5)
        self.assertEqual(s["probabilities"]["drawdown_gt_20_pct"],.5)
        self.assertEqual(s["sequence_risk"]["worst_simulated_loss_streak"],4)


class Bootstrap(unittest.TestCase):
    def test_seed_reproducibility_and_actual_observations(self):
        a=bootstrap_indices(random.Random(7),100)
        self.assertEqual(a,bootstrap_indices(random.Random(7),100))
        self.assertNotEqual(a,bootstrap_indices(random.Random(8),100))
        self.assertTrue(all(0<=v<100 for v in a))

    def test_blocks_preserve_contiguous_dates_and_empty_days(self):
        indices=bootstrap_indices(random.Random(19),20,4)
        for start in range(0,20,4):
            self.assertEqual(indices[start:start+4],list(range(indices[start],indices[start]+4)))
        tl=TimelineFixture()
        pool=[trade("A"),trade("B",entry=25)]
        result=block_sample(pool,tl,[1]*8)
        self.assertFalse(result)
        result=block_sample(pool,tl,[0]+[1]*7)
        self.assertEqual([t["symbol"] for t in result],["A","B"])

    def test_portfolio_mc_seed_quantiles_and_10000_paths(self):
        c=configuration();c["paths"]=10000
        tl=TimelineFixture(4)
        a=monte_carlo([trade()],tl,c,IDEAL,.005,"TRADE_IID",technical_test=True)
        self.assertEqual(a["completed_paths"],10000)
        self.assertEqual(a["median_end_capital"],10100)
        self.assertEqual(a["end_capital_quantiles"]["P01"],10100)
        c["paths"]=100
        b=monte_carlo([trade(),trade("B",entry=100,exit_mid=95)],tl,c,IDEAL,.005,"TRADE_IID",technical_test=True)
        self.assertEqual(b,monte_carlo([trade(),trade("B",entry=100,exit_mid=95)],tl,c,IDEAL,.005,"TRADE_IID",technical_test=True))

    def test_block_mc_and_no_pool(self):
        c=configuration();tl=TimelineFixture(4)
        a=monte_carlo([trade()],tl,c,IDEAL,.005,"SESSION_BLOCK",technical_test=True)
        self.assertEqual(a["completed_paths"],100)
        self.assertEqual(a,monte_carlo([trade()],tl,c,IDEAL,.005,"SESSION_BLOCK",technical_test=True))
        self.assertEqual(monte_carlo([],tl,c,IDEAL,.005,"TRADE_IID")["completed_paths"],0)
        self.assertEqual(monte_carlo([trade()],TimelineFixture(2),c,IDEAL,.005,"SESSION_BLOCK",technical_test=True)["status"],"UNAVAILABLE_FEWER_THAN_TWO_TIME_BLOCKS")

    def test_true_trade_sample_thresholds_and_no_five_trade_strategy_monte_carlo(self):
        self.assertEqual([true_trade_sample_status(n) for n in (0,5,29,30,99,100)],
                         ["VERY_LOW_SAMPLE"]*3+["LOW_SAMPLE"]*2+["REGULAR_EXPLORATORY"])
        c=configuration()
        result=monte_carlo([trade()],TimelineFixture(),c,IDEAL,.005,"TRADE_IID")
        self.assertEqual(result["completed_paths"],0)
        self.assertEqual(result["status"],"VERY_LOW_SAMPLE")


class Barriers(unittest.TestCase):
    def obs(self, values):
        return [{"day":"2026-01-%02d"%(5+i),"equity":v} for i,v in enumerate(values)]

    def test_never_hit_is_none_not_false_argmax_zero(self):
        self.assertIsNone(first_hit([False,False]))
        self.assertIsNone(first_hit([]))
        self.assertEqual(first_hit([True,False]),0)
        a=evaluate_challenge(self.obs([100,101,102]),100,{"profit_target":.1,"maximum_total_loss":.1})
        self.assertEqual(a["status"],"NO_HIT")
        self.assertIsNone(a["profit_target_index"])
        self.assertIsNone(a["loss_limit_index"])

    def test_target_first_and_loss_first(self):
        r={"profit_target":.1,"maximum_total_loss":.1}
        a=evaluate_challenge(self.obs([100,110,80]),100,r)
        self.assertEqual(a["status"],"TARGET_FIRST")
        self.assertEqual(a["profit_target_index"],1)
        b=evaluate_challenge(self.obs([100,89,120]),100,r)
        self.assertEqual(b["status"],"LOSS_FIRST")
        self.assertEqual(b["loss_limit_index"],1)

    def test_only_loss_hit_and_only_target_hit(self):
        r={"profit_target":.1,"maximum_total_loss":.1}
        self.assertEqual(evaluate_challenge(self.obs([100,80]),100,r)["status"],"LOSS_FIRST")
        self.assertEqual(evaluate_challenge(self.obs([100,120]),100,r)["status"],"TARGET_FIRST")

    def test_daily_trailing_and_min_days(self):
        obs=[{"day":"2026-01-05","equity":100},{"day":"2026-01-05","equity":94}]
        self.assertEqual(evaluate_challenge(obs,100,{"maximum_daily_loss":.05})["status"],"LOSS_FIRST")
        self.assertEqual(evaluate_challenge(self.obs([100,120,108]),100,{"trailing_drawdown":.1})["status"],"LOSS_FIRST")
        obs=self.obs([100,120]);obs[1]["is_entry"]=True
        self.assertEqual(evaluate_challenge(obs,100,{"profit_target":.1,"minimum_trading_days":2})["status"],"NO_HIT")

    def test_calendar_period_and_max_trade_risk(self):
        obs=[{"day":"2026-01-05","equity":100},{"day":"2026-01-20","equity":120}]
        self.assertEqual(evaluate_challenge(obs,100,{"profit_target":.1,"challenge_period_days":10})["status"],"NO_HIT")
        obs=self.obs([100]);obs[0].update(is_entry=True,risk_per_trade=.02)
        self.assertEqual(evaluate_challenge(obs,100,{"maximum_risk_per_trade":.01})["status"],"LOSS_FIRST")

    def test_simultaneous_eligible_target_and_trailing_loss_is_loss_first(self):
        obs=self.obs([100,130,117]);obs[-1]["is_entry"]=True
        result=evaluate_challenge(obs,100,{"profit_target":.1,"trailing_drawdown":.1,"minimum_trading_days":1})
        self.assertEqual(result["profit_target_index"],2)
        self.assertEqual(result["loss_limit_index"],2)
        self.assertEqual(result["status"],"LOSS_FIRST")

    def test_missing_entry_risk_and_nonchronological_data_fail(self):
        obs=self.obs([100]);obs[0]["is_entry"]=True
        with self.assertRaises(ValueError):evaluate_challenge(obs,100,{"maximum_risk_per_trade":.01})
        with self.assertRaises(ValueError):evaluate_challenge(self.obs([100,100])[::-1],100,{"profit_target":.1})


class CurrencyAndRegression(unittest.TestCase):
    def test_fx_uses_completed_observation_and_staleness(self):
        t=int(dt.datetime(2026,1,5,tzinfo=dt.timezone.utc).timestamp())
        raw={"meta":{"symbol":"EURUSD=X","dataGranularity":"1d","currency":"USD"},"timestamp":[t,t+86400],"indicators":{"quote":[{"close":[1.1,1.2]}]}}
        f=FX("EUR",raw)
        self.assertEqual(f.at("2026-01-06T12:00:00Z"),1.1)
        with self.assertRaises(ValueError):f.at("2026-01-05T12:00:00Z")
        with self.assertRaises(ValueError):f.at("2026-01-20T12:00:00Z")
        with self.assertRaises(ValueError):FX("EUR")
        raw["meta"]["dataGranularity"]="3mo"
        with self.assertRaises(ValueError):FX("EUR",raw)

    def test_existing_strategies_risk_and_native_config_unchanged(self):
        frozen=read(Path(ROOT)/"trader-backtest/strategy-sources.json")
        for path,sha in frozen["sha256"].items():
            self.assertEqual(hashlib.sha256((Path(ROOT)/path).read_bytes()).hexdigest(),sha,path)
        c=read(Path(ROOT)/"trader-backtest/config.first.json")
        self.assertEqual(c["target_r"],2)
        self.assertEqual(c["max_hold_sessions"],1)


if __name__=="__main__":
    unittest.main()
