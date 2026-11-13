"""Adversarial timing/funding tests for next-open event execution."""
import sys
from pathlib import Path
import unittest
import copy
import datetime as dt
import gzip
import json
import tempfile
import numpy as np
from _load import ROOT
sys.path.insert(0,str(Path(ROOT)/"trader-backtest"))
from event_portfolio_book import historical
from test_event_portfolio import offers
from event_portfolio_audit import tail_variant,benchmark


class CausalExecution(unittest.TestCase):
    def test_no_open_inference_from_close(self):
        with self.assertRaisesRegex(ValueError,"opening"):
            historical(np.array([[1.,1.1]]),offers(3),np.array([0]),1,1,10000,0)

    def test_open_budget_never_uses_todays_future_close(self):
        stream=offers(4);stream[0,0]=0;stream[1,0]=1
        opens=np.array([[1,1,1],[1,1,1]],dtype=float)
        paths=np.array([[1,1,100,1],[1,1,1,1]],dtype=float)
        a=historical(paths,stream,np.array([0,1]),2,3,10000,0,open_paths=opens)
        paths[0,2]=.01
        b=historical(paths,stream,np.array([0,1]),2,3,10000,0,open_paths=opens)
        self.assertEqual(a[7][1,4],5000)
        self.assertEqual(a[7][1,4],b[7][1,4])

    def test_unrealized_gains_do_not_increase_spendable_cash(self):
        stream=offers(4);stream[0,0]=0;stream[1,0]=1
        opens=np.array([[1,3,3],[1,1,1]],dtype=float)
        closes=np.array([[1,3,3,3],[1,1,1,1]],dtype=float)
        book=historical(closes,stream,np.array([0,1]),2,3,10000,0,open_paths=opens)
        self.assertEqual(book[7][1,2],20000)  # marked current equity
        self.assertEqual(book[7][1,8],10000)  # half-equity target
        self.assertEqual(book[7][1,3],5000)   # only actual cash is available
        self.assertEqual(book[7][1,4],5000)
        self.assertEqual(book[6][1,4],0)

    def test_closing_proceeds_cannot_fund_same_morning_or_overwrite_ticker(self):
        stream=offers(5);stream[0,0]=0;stream[1,0]=1;stream[2,0]=2
        paths=np.array([[1,1,2],[1,1,1],[1,1,1]],dtype=float)
        opens=np.ones((3,2))
        book=historical(paths,stream,np.array([7,7,7]),1,2,10000,0,open_paths=opens)
        self.assertEqual(book[5].tolist(),[1,0,1])
        self.assertEqual(book[7][0,1],1)
        self.assertEqual(book[7][2,0],2)
        self.assertEqual(book[7][2,3],20000)
        self.assertEqual(book[0][14],0)

    def test_exact_twenty_sessions_entry_is_session_one(self):
        stream=offers(25);stream[1,0]=0
        book=historical(np.ones((1,21)),stream,np.array([0]),1,20,10000,0,open_paths=np.ones((1,20)))
        self.assertEqual(book[7][0,0],1)
        self.assertEqual(book[7][0,1],20)
        self.assertEqual(book[7][0,1]-book[7][0,0]+1,20)
        self.assertEqual(book[6][19,11],1)
        self.assertEqual(book[6][20,11],0)

    def test_five_and_ten_slots_allocations_cash_and_exposure(self):
        for slots in (5,10):
            stream=offers(5,2*slots);stream[0,:slots]=np.arange(slots)
            p=np.ones((slots,3));p[:,1]=2;p[:,2]=.25
            op=np.ones((slots,2));op[:,1]=1.5
            book=historical(p,stream,np.arange(slots),slots,2,10000,.001,open_paths=op)
            ledger=book[7];used=book[5].astype(bool);d=book[6]
            np.testing.assert_allclose(ledger[used,10],1/slots)
            np.testing.assert_allclose(ledger[used,5]/ledger[used,2],1/slots/1.001)
            self.assertTrue(np.all(d[:,4]>=0) and np.all(d[:,8]>=0))
            np.testing.assert_allclose(d[:,8],d[:,0]-d[:,12]+d[:,13],atol=1e-9)
            np.testing.assert_allclose(d[:,10],d[:,8]+d[:,9])
            self.assertLessEqual(book[0][13],1+1e-12)
            self.assertLessEqual(book[0][4],slots)

    def test_idle_cash_has_zero_interest(self):
        book=historical(np.ones((1,2)),offers(100),np.array([0]),5,1,10000,.001,open_paths=np.ones((1,1)))
        np.testing.assert_array_equal(book[1],np.full(101,10000))
        np.testing.assert_array_equal(book[6][:,8],np.full(100,10000))


class AuditSensitivityAndBenchmark(unittest.TestCase):
    def pool(self):
        r=np.array([.1,.2,.3,.4,10.])
        return {"paths":np.c_[np.ones(5),1+r/2,1+r],"open_paths":np.c_[np.ones(5),1+r/3],
                "quality":np.zeros(5,dtype="uint8"),"days":np.arange(5),"assets":np.arange(5)}

    def test_tail_scenarios_do_not_modify_source_or_ranking(self):
        pool=self.pool();before=copy.deepcopy(pool)
        removed,desc=tail_variant(pool,"REMOVE_TOP_0.1_PERCENT")
        self.assertEqual(desc["changed_events"],1)
        np.testing.assert_array_equal(removed["assets"],[0,1,2,3])
        for kind in ("WINSOR_P99.9","WINSOR_P99"):
            derived,details=tail_variant(pool,kind)
            self.assertLessEqual(np.max(derived["paths"][:,-1]-1),details["gross_return_cap_pct"]/100+1e-12)
            np.testing.assert_array_equal(derived["assets"],pool["assets"])
            np.testing.assert_allclose(derived["paths"][:,0],1)
            self.assertFalse(details["raw_quotes_changed"])
        for key in before:np.testing.assert_array_equal(pool[key],before[key])

    def test_benchmark_matches_dates_costs_and_never_fabricates_preinception(self):
        dates=["2026-01-05","2026-01-06","2026-01-07","2026-01-08","2026-01-09"]
        meta={"days":dates,"test_last_index":4}
        close=[100,110,90,100,120]
        raw={"meta":{"symbol":"QQQ","currency":"USD","dataGranularity":"1d"},
            "timestamp":[int(dt.datetime.fromisoformat(d+"T14:30:00+00:00").timestamp()) for d in dates],
            "indicators":{"quote":[{"open":[100]*5,"high":[max(100,c)+1 for c in close],
                "low":[min(100,c)-1 for c in close],"close":close,"volume":[1000]*5}]}}
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"QQQ.json.gz"
            p.write_bytes(gzip.compress(json.dumps({"chart":{"result":[raw]}}).encode()))
            result,curve,_=benchmark(p,meta,0)
            self.assertEqual(result["start"],dates[0]);self.assertEqual(result["end"],dates[-1])
            self.assertAlmostEqual(result["end_capital"],10000/1.001*1.2*.999)
            self.assertAlmostEqual(result["max_drawdown"],1-90/110)
            raw["timestamp"]=raw["timestamp"][2:]
            for key in raw["indicators"]["quote"][0]:raw["indicators"]["quote"][0][key]=raw["indicators"]["quote"][0][key][2:]
            p.write_bytes(gzip.compress(json.dumps({"chart":{"result":[raw]}}).encode()))
            result,curve,first=benchmark(p,meta,0)
            self.assertEqual(result["status"],"UNAVAILABLE_BEFORE_INCEPTION")
            self.assertIsNone(curve);self.assertEqual(first,2)
            matched,_,_=benchmark(p,meta,first)
            self.assertEqual(matched["start"],dates[2])


if __name__=="__main__":unittest.main()
