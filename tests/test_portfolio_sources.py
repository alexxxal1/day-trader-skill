"""Causal event/trigger linkage and offline end-to-end portfolio reporting."""
import copy
import datetime as dt
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from _load import ROOT
sys.path.insert(0,str(Path(ROOT)/"trader-backtest"))
from common import Clock, digest, iso, read, stamp
from data import MarketData
from portfolio_model import FX
from portfolio_run import run
from portfolio_sources import Timeline, bind_plans, forward_events, reconstruct
from test_backtest import config as native_config, document, plan
from test_portfolio import configuration


def fixture():
    doc=document()
    clock=Clock(doc["sessions"])
    for day in ("2026-09-30","2026-10-01","2026-10-02"):
        start=clock.eastern_to_utc(dt.datetime.combine(dt.date.fromisoformat(day),dt.time(9,30)))
        bars=[]
        for i in range(78):
            begin=start+dt.timedelta(minutes=5*i)
            end=begin+dt.timedelta(minutes=5)
            bars.append(dict(start=iso(begin),end=iso(end),available_at=iso(end),o=100,h=101,l=99,c=100,v=10000))
        for sym in ("A","B"):
            doc["bars"][sym].setdefault("5m",[]).extend(copy.deepcopy(bars))
    event={"id":"COOLDOWN20/MINERVINI/A/2026-09-30","strategy":"MINERVINI","symbol":"A",
        "day":"2026-09-30","at":iso(clock.close("2026-09-30")),"RS_PROXY":90,"liquidity":1e6,"status":"PASS"}
    days=["2026-09-30","2026-10-01","2026-10-02"]
    source={"days":days,"universe":["A","B"],"states":np.full((3,2),21,dtype="uint8"),
        "eligible":np.ones((3,2),dtype=bool),"events":{"COOLDOWN20":[event],"FULL_RESET":[]}}
    p=plan(at="2026-10-01T13:50:00Z")
    return doc,source,p


class EventTradeLinkage(unittest.TestCase):
    def test_first_post_event_trigger_once(self):
        doc,s,p=fixture();m=MarketData(doc)
        later=dict(p,id="second",at="2026-10-01T14:00:00Z")
        linked,missing=bind_plans(s,[later,p],"COOLDOWN20",m)
        self.assertEqual(len(linked),1)
        self.assertEqual(linked[0]["trigger_id"],p["id"])
        self.assertFalse(missing)

    def test_trigger_before_event_cannot_be_backdated(self):
        doc,s,p=fixture();m=MarketData(doc)
        s["events"]["COOLDOWN20"][0].update(day="2026-10-01",at="2026-10-01T20:00:00Z")
        linked,missing=bind_plans(s,[p],"COOLDOWN20",m)
        self.assertFalse(linked)
        self.assertEqual(len(missing),1)

    def test_no_late_replacement_after_missing_intraday_prefix(self):
        doc,s,p=fixture();doc["bars"]["A"]["5m"]=[r for r in doc["bars"]["A"]["5m"] if r["start"]!="2026-10-01T13:30:00Z"]
        self.assertFalse(bind_plans(s,[p],"COOLDOWN20",MarketData(doc))[0])

    def test_reset_or_unknown_invalidates_event_before_later_trigger(self):
        for unknown in (False,True):
            doc,s,p=fixture();p.update(day="2026-10-02",at="2026-10-02T13:50:00Z",stop_known_at="2026-10-02T13:50:00Z")
            if unknown:s["eligible"][1,0]=False
            else:s["states"][1,0]=0
            self.assertFalse(bind_plans(s,[p],"COOLDOWN20",MarketData(doc))[0])

    def test_complete_path_native_exit_and_future_stop_rejection(self):
        doc,s,p=fixture();m=MarketData(doc)
        linked,_=bind_plans(s,[p],"COOLDOWN20",m)
        tl=Timeline(m,"2026-09-30","2026-10-02")
        good,bad=reconstruct(m,linked,native_config(),FX("USD"),tl)
        self.assertEqual(good[0]["exit_reason"],"TIME_EXIT")
        self.assertEqual(good[0]["exit_at"],"2026-10-01T19:55:00Z")
        self.assertFalse(bad)
        linked[0]["stop_known_at"]="2026-10-01T14:00:00Z"
        self.assertFalse(reconstruct(m,linked,native_config(),FX("USD"),tl)[0])

    def test_hole_after_entry_is_not_an_invented_exit(self):
        doc,s,p=fixture();m=MarketData(doc)
        linked,_=bind_plans(s,[p],"COOLDOWN20",m)
        doc["bars"]["A"]["5m"]=[r for r in doc["bars"]["A"]["5m"] if r["start"]!="2026-10-01T15:00:00Z"]
        m=MarketData(doc)
        good,bad=reconstruct(m,linked,native_config(),FX("USD"),Timeline(m,"2026-09-30","2026-10-02"))
        self.assertFalse(good)
        self.assertEqual(bad[0]["reason"],"INCOMPLETE_POSITION_HISTORY")

    def test_forward_only_no_stop_or_r_is_fabricated(self):
        doc,s,p=fixture();m=MarketData(doc)
        rows=forward_events(m,s["events"]["COOLDOWN20"],1,"2026-10-02")
        self.assertIsNotNone(rows[0]["return_pct"])
        self.assertNotIn("r_multiple",rows[0])
        self.assertNotIn("stop",rows[0])
        self.assertEqual(forward_events(m,s["events"]["COOLDOWN20"],20,"2026-10-02")[0]["issue"],"RIGHT_CENSORED")


class OfflineRun(unittest.TestCase):
    def test_end_to_end_and_frozen_repeat(self):
        doc,s,p=fixture();nc=native_config();nc.update(start="2026-09-30",end="2026-10-02")
        cfg=configuration();cfg.update(paths=10,event_variants=["COOLDOWN20"],risk_per_trade=[.005],forward_horizon=1)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("portfolio_run.daily_events",return_value=s),patch("portfolio_run.replay",return_value={"plans":[p],"rejections":[]}):
                a=run(doc,nc,cfg,tmp)
            b=run(doc,nc,cfg,tmp)
            self.assertEqual(digest(a),digest(b))
            mini=next(r for r in a["results"] if r["strategy"]=="MINERVINI")
            self.assertEqual(mini["historical"]["trades"],1)
            unavailable=next(r for r in a["results"] if r["strategy"]=="CONFLUENCE_SETUP_READY")
            self.assertIsNone(unavailable["historical"]["end_capital"])
            self.assertEqual(unavailable["monte_carlo"]["TRADE_IID"]["completed_paths"],0)
            self.assertTrue((Path(tmp)/"REPORT.md").exists())
            cfg["seed"]+=1
            with self.assertRaises(ValueError):run(doc,nc,cfg,tmp)

    def test_existing_backtest_code_and_configs_unchanged(self):
        frozen=read(Path(ROOT)/"tests/portfolio-existing-sources.json")
        for path,sha in frozen["sha256"].items():
            self.assertEqual(hashlib.sha256((Path(ROOT)/path).read_bytes()).hexdigest(),sha,path)


if __name__=="__main__":unittest.main()
