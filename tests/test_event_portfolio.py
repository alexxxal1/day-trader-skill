"""Synthetic event-return books, independent expected P&L, resampling and QC."""
import copy
import datetime as dt
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

from _load import ROOT
sys.path.insert(0,str(Path(ROOT)/"trader-backtest"))
from common import read,write
from daily_history import mapped
from event_portfolio_book import historical as historical_engine,bootstrap as bootstrap_engine,ACCELERATOR
from event_portfolio_data import catalog,panel
from event_portfolio_run import history_metrics,monte_metrics,true_trade_inventory
from event_rehydrate import freeze


def offers(n=8,width=4):
    return np.full((n,width),-1,dtype="int32")


def gapless_synthetic_opens(paths):
    """Explicit zero-overnight-gap assumption for synthetic test fixtures only."""
    paths=np.asarray(paths)
    return np.c_[paths[:,0],paths[:,1:-1]]


def historical(paths,*args,**kwargs):
    kwargs.setdefault("open_paths",gapless_synthetic_opens(paths))
    return historical_engine(paths,*args,**kwargs)


def bootstrap(paths,*args,**kwargs):
    kwargs.setdefault("open_paths",gapless_synthetic_opens(paths))
    return bootstrap_engine(paths,*args,**kwargs)


class DailyEventBook(unittest.TestCase):
    def test_equal_initial_weights_drift_and_cash_reconcile(self):
        paths=np.array([[1,1.1,1.2],[1,.95,.9]])
        stream=offers();stream[0,:2]=[0,1];stream[3,:2]=[0,1]
        result=historical(paths,stream,np.array([0,1]),2,2,10000,0)
        values,curve,positions,invested,pnl,accepted=result[:6]
        # Two equal allocations gain +20%/-10% => portfolio +5% per cycle.
        self.assertEqual(values[0],10000*1.05*1.05)
        self.assertEqual(values[4],2)
        self.assertEqual(values[5],4)
        self.assertEqual(values[0]-10000,pnl.sum())
        self.assertEqual(values[11],values[0])
        np.testing.assert_allclose(curve,[10000,10250,10500,10500,10762.5,11025,11025,11025,11025])

    def test_daily_marks_include_unrecovered_drawdown_not_only_endpoint(self):
        stream=offers(4);stream[0,0]=0
        v,*_=historical(np.array([[1,.8,1]]),stream,np.array([0]),1,2,10000,0)
        self.assertEqual(v[0],10000)
        self.assertAlmostEqual(v[1],.2)

    def test_spread_slippage_costs_once_at_entry_and_exit(self):
        stream=offers(4);stream[0,0]=0
        v,*_=historical(np.array([[1,1,1]]),stream,np.array([0]),1,2,10000,.001)
        expected=10000*.999/1.001
        self.assertAlmostEqual(v[0],expected)
        self.assertAlmostEqual(v[7],10000-expected)
        self.assertGreaterEqual(v[11],0)

    def test_slots_and_predefined_offer_order(self):
        stream=offers(4,6);stream[0,:3]=[2,1,0]
        paths=np.array([[1,.5],[1,1],[1,1.1]])
        result=historical(paths,stream,np.array([0,1,2]),1,1,10000,0)
        v,selected=result[0],result[5]
        self.assertEqual(v[0],11000)
        np.testing.assert_array_equal(selected,[0,0,1])
        self.assertEqual(v[4],1)

    def test_no_duplicate_ticker_even_different_event_ids(self):
        stream=offers(5);stream[0,0]=0;stream[1,:2]=[1,2]
        paths=np.array([[1,1,1],[1,1,1],[1,1,1]])
        result=historical(paths,stream,np.array([7,7,8]),2,2,10000,0)
        v,selected=result[0],result[5]
        self.assertEqual(v[5],2)
        self.assertEqual(v[10],1)
        np.testing.assert_array_equal(selected,[1,0,1])

    def test_no_right_censored_positions_or_invented_exit(self):
        stream=offers(4);stream[3,0]=0
        v,*_=historical(np.array([[1,2,3]]),stream,np.array([0]),1,2,10000,0)
        self.assertEqual(v[0],10000)
        self.assertEqual(v[5],0)

    def test_metrics_are_events_not_risk_trades(self):
        stream=offers(4);stream[0,0]=0
        paths=np.array([[1,1.1]])
        book=historical(paths,stream,np.array([0]),1,1,10000,0)
        days=["2026-01-05","2026-01-06","2026-01-07","2026-01-08"]
        m=history_metrics(book,paths,days,10000)
        self.assertEqual(m["measurement"],"EVENT_RETURN_PORTFOLIO")
        self.assertIsNone(m["risk_per_trade"])
        self.assertIsNone(m["stop_based_r_multiple"])
        self.assertEqual(m["closed_event_positions"],1)
        self.assertEqual(m["event_pool_distribution"]["tail"]["largest_one_percent_n"],1)

    def test_bad_missing_or_nonpositive_paths_are_rejected(self):
        stream=offers(4);stream[0,0]=0
        for path in ([1,np.nan],[1,0],[2,3]):
            with self.assertRaises(ValueError):historical(np.array([path]),stream,np.array([0]),1,1,10000,0)


class EmpiricalEventBootstrap(unittest.TestCase):
    def test_seed_reproducibility_and_actual_large_pool_including_unadmitted(self):
        stream=offers(4);stream[0,:2]=[0,1]
        paths=np.array([[1,.9],[1,2.]])
        a=bootstrap(paths,stream,np.array([0,1]),1,1,10000,0,"IID_EVENT_PATH",7,2,100)
        b=bootstrap(paths,stream,np.array([0,1]),1,1,10000,0,"IID_EVENT_PATH",7,2,100)
        np.testing.assert_array_equal(a,b)
        self.assertEqual(set(a[:,0]),{9000,20000})
        self.assertFalse(np.array_equal(a,bootstrap(paths,stream,np.array([0,1]),1,1,10000,0,"IID_EVENT_PATH",8,2,100)))

    def test_blocks_keep_whole_cross_section_and_idle_sessions(self):
        stream=offers(12);stream[1,:2]=[0,1]
        paths=np.array([[1,1.1,1.2],[1,.9,.8]])
        block=bootstrap(paths,stream,np.array([0,1]),2,2,10000,0,"SESSION_BLOCK",19,3,100)
        iid=bootstrap(paths,stream,np.array([0,1]),2,2,10000,0,"IID_EVENT_PATH",19,3,100)
        np.testing.assert_allclose(block[:,0],10000)
        np.testing.assert_allclose(block[:,1],0)
        self.assertTrue(np.any(block[:,5]==0))
        self.assertGreater(np.max(iid[:,1]),0)
        self.assertLessEqual(np.max(block[:,4]),2)

    def test_10000_reproducible_paths_and_report_probabilities(self):
        stream=offers(4);stream[0,:2]=[0,1]
        paths=np.array([[1,.9],[1,2.]])
        a=bootstrap(paths,stream,np.array([0,1]),1,1,10000,0,"IID_EVENT_PATH",20261111,2,10000)
        self.assertEqual(a.shape,(10000,17))
        c=read(Path(ROOT)/"trader-backtest/event-portfolio-config.json")
        r=monte_metrics(a,10000,"IID_EVENT_PATH",c)
        self.assertAlmostEqual(r["probabilities"]["negative_total_performance"],np.mean(a[:,0]<10000))
        self.assertAlmostEqual(r["probabilities"]["gain_ge_100_pct"],np.mean(a[:,0]>=20000))
        self.assertEqual(r["end_capital_quantiles"]["P01"],9000)
        self.assertEqual(r["end_capital_quantiles"]["P99"],20000)
        self.assertEqual(r["measurement"],"EVENT_RETURN_PORTFOLIO")

    def test_short_time_axis_blocks_fail_explicitly(self):
        stream=offers(4)
        with self.assertRaises(ValueError):bootstrap(np.array([[1,1]]),stream,np.array([0]),1,1,10000,0,"SESSION_BLOCK",0,3,10)

    @unittest.skipUnless(ACCELERATOR=="numba","optional compiled accelerator")
    def test_seed_is_independent_of_worker_thread_count(self):
        from numba import set_num_threads,get_num_threads
        previous=get_num_threads()
        stream=offers(12);stream[1,:2]=[0,1];stream[5,:2]=[0,1]
        paths=np.array([[1,1.1,1.2],[1,.9,.8]])
        try:
            for method in ("IID_EVENT_PATH","SESSION_BLOCK"):
                set_num_threads(1)
                a=bootstrap(paths,stream,np.array([0,1]),2,2,10000,.001,method,19,3,100)
                set_num_threads(min(previous,2))
                b=bootstrap(paths,stream,np.array([0,1]),2,2,10000,.001,method,19,3,100)
                np.testing.assert_array_equal(a,b)
        finally:
            set_num_threads(previous)

    def test_true_trade_inventory_never_uses_daily_signals_as_true_trades(self):
        r=true_trade_inventory()
        self.assertEqual(len(r),3)
        for row in r.values():
            self.assertEqual(row["status"],"INSUFFICIENT_TRADE_DATA")
            self.assertEqual(row["monte_carlo"]["paths"],0)


def tiny_event_data(base):
    a,e=base/"analysis",base/"events"
    a.mkdir();e.mkdir();(a/"stock-cache").mkdir()
    days=[str(dt.date(2026,1,5)+dt.timedelta(days=i)) for i in range(8)]
    meta={"shape":[8,3],"days":days,"universe":["A","B","C"],"test_first_index":0,"test_last_index":7,"acquisition_sha256":"synthetic"}
    write(a/"matrix-metadata.json",meta);write(e/"event-metadata.json",meta)
    write(e/"frozen-methodology.json",read(Path(ROOT)/"trader-backtest/event-methodology.json"))
    c=mapped(a/"close.f64",[8,3],"float64","w+");c[:]=100+np.arange(8)[:,None];c.flush()
    after=mapped(a/"forward-contiguous.u8",[8,3],"uint8","w+");after[:]=np.arange(7,-1,-1)[:,None];after.flush()
    ranks=mapped(a/"ranks.f64",[4,8,3],"float64","w+");ranks[:]=[90,90,100];ranks.flush()
    ev=mapped(e/"events-COOLDOWN20.u8",[8,3],"uint8","w+");ev[:]=0;ev[0,:]=21;ev[6,0]=21;ev.flush()
    q=mapped(e/"quality-window-2.u8",[8,3],"uint8","w+");q[:]=0;q[0,2]=1;q.flush()
    for i,sym in enumerate(meta["universe"]):np.savez_compressed(a/"stock-cache"/(sym+".npz"),c=c[:,i],o=c[:,i]+.25,v=np.full(8,(i+1)*1000))
    return a,e


class FixedEventCohorts(unittest.TestCase):
    def test_freeze_survives_json_tuple_list_roundtrip_and_rejects_real_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"frozen.json"
            freeze(path,{"kept":[("A","Name")]})
            freeze(path,{"kept":[("A","Name")]})
            with self.assertRaises(ValueError):freeze(path,{"kept":[("B","Name")]})

    def test_quality_filter_ranking_censoring_and_cached_reproduction(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);a,e=tiny_event_data(base)
            pool,count=catalog(a,e,base/"catalog","MINERVINI",2)
            self.assertEqual(count["native_cooldown_events"],4)
            self.assertEqual(count["right_censored"],1)
            self.assertEqual(count["quality_counts"],{"VALID":2,"SUSPECT":1,"INVALID":0})
            np.testing.assert_array_equal(pool["assets"],[2,1,0])
            np.testing.assert_allclose(pool["paths"],[[1,101/101.25,102/101.25]]*3)
            np.testing.assert_allclose(pool["open_paths"],[[1,102.25/101.25]]*3)
            p,o,assets,indices=panel(pool,"VALID_ONLY",8,2)
            np.testing.assert_array_equal(assets,[1,0])
            np.testing.assert_array_equal(o[1,:2],[0,1])
            np.testing.assert_array_equal(o[0],[-1]*4)
            self.assertEqual(len(panel(pool,"ALL",8,2)[0]),3)
            cached,_=catalog(a,e,base/"catalog","MINERVINI",2)
            np.testing.assert_array_equal(cached["paths"],pool["paths"])


if __name__=="__main__":unittest.main()
