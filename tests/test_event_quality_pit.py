"""Adversarial information-clock and independent quality-panel tests."""
import copy
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
from _load import ROOT
sys.path.insert(0,str(Path(ROOT)/"trader-backtest"))
from common import read,write
from data_quality import classify
from event_portfolio_book import historical
from event_portfolio_data import panel
from event_portfolio_run import run as full_portfolio_run
from event_quality_pit import (CAUSAL_BITS,CAUSAL_INVALID_BITS,VINTAGE_STATUS,
    annotate_pools,asof_revisions,causal_bar_flags,quality_mask,quote_arrays,rule_inventory,window_bits)

CONFIG=read(Path(ROOT)/"trader-backtest/event-methodology.json")["quality"]


def bars(n=300):
    return {"o":np.full(n,100.),"h":np.full(n,101.),"l":np.full(n,99.),
            "c":np.full(n,100.),"v":np.full(n,1000.),"present":np.ones(n,dtype=bool)}


class CausalQualityTests(unittest.TestCase):
    def test_no_future_ohlcv_can_change_an_earlier_decision(self):
        a=bars();b=copy.deepcopy(a)
        b["c"][281:]=0;b["v"][281:]=-1;b["h"][281:]=np.nan
        before=window_bits(causal_bar_flags(a,CONFIG),[280])
        after=window_bits(causal_bar_flags(b,CONFIG),[280])
        np.testing.assert_array_equal(before,after)
        self.assertEqual(int(after[0]),0)

    def test_whole_prefix_invariance_over_every_signal_date(self):
        b=bars(40);b['c'][10:]=300;b['h'][10:]=301;b['l'][10:]=299;b['o'][10:]=300
        b['v'][13:]=0
        complete=causal_bar_flags(b,CONFIG)
        for t in range(1,40):
            prefix={k:v[:t] for k,v in b.items()}
            np.testing.assert_array_equal(causal_bar_flags(prefix,CONFIG),complete[:t])

    def test_original_thresholds_not_tuned(self):
        b=bars(5);b['c'][:]=[100,199,398,199,199];b['h']=b['c']+1;b['l']=b['c']-1;b['o']=b['c'].copy()
        f=causal_bar_flags(b,CONFIG)
        np.testing.assert_array_equal(f&2,[0,0,2,2,0])

    def test_numeric_invalid_and_suspect_are_distinct(self):
        b=bars(4);b['v'][1]=0;b['c'][1]=100.1;b['o'][2]=102
        f=causal_bar_flags(b,CONFIG)
        self.assertEqual(f[1],4);self.assertEqual(f[2],1)
        self.assertEqual(f[1]&CAUSAL_INVALID_BITS,0)
        self.assertNotEqual(f[2]&CAUSAL_INVALID_BITS,0)

    def test_stale_threshold_twenty_trailing_sessions(self):
        b=bars(24);b['v'][:]=0;f=causal_bar_flags(b,CONFIG)
        self.assertEqual(f[19]&8,0);self.assertEqual(f[20]&8,8)

    def test_missing_bar_is_not_silently_clean(self):
        b=bars(3);b['present'][1]=False
        for k in ('o','h','l','c','v'):b[k][1]=np.nan
        self.assertEqual(causal_bar_flags(b,CONFIG)[1],2048)

    def test_feature_window_is_260_including_signal(self):
        f=np.zeros(301,dtype=np.uint32);f[40]=2
        self.assertEqual(window_bits(f,[299])[0],2)
        self.assertEqual(window_bits(f,[300])[0],0)

    def test_no_later_action_sec_identity_or_adjclose_can_affect_kernel(self):
        b=bars();base=causal_bar_flags(b,CONFIG)
        b.update(events={'splits':{'later':{'numerator':1,'denominator':100000}}},
                 meta={'firstTradeDate':99999999999,'currency':'EUR'},
                 adjclose=np.full(300,np.nan),sec_reconciliation={'future_reuse':True})
        np.testing.assert_array_equal(causal_bar_flags(b,CONFIG),base)

    def test_reproducible_and_does_not_mutate_prices(self):
        b=bars();b['v'][250:]=0;before=copy.deepcopy(b)
        np.testing.assert_array_equal(causal_bar_flags(b,CONFIG),causal_bar_flags(b,CONFIG))
        for k in before:np.testing.assert_array_equal(b[k],before[k])

    def test_all_pit_valid_and_invalid_only_remain_separate(self):
        pool={'days':np.arange(4),'quality':np.array([0,1,0,2]),'pit_reasons':np.array([0,0,2,1])}
        self.assertEqual(quality_mask(pool,'ALL').tolist(),[True]*4)
        self.assertEqual(quality_mask(pool,'PIT_SAFE').tolist(),[True,True,False,False])
        self.assertEqual(quality_mask(pool,'VALID_ONLY').tolist(),[True,False,True,False])
        self.assertEqual(quality_mask(pool,'INVALID_ONLY_PIT').tolist(),[True,True,True,False])

    def test_pit_never_falls_back_to_retrospective_valid(self):
        with self.assertRaisesRegex(ValueError,'annotations required'):
            quality_mask({'days':np.arange(2),'quality':np.zeros(2)},'PIT_SAFE')

    def test_future_legacy_flags_do_not_change_pit_membership(self):
        pool={'days':np.arange(2),'quality':np.array([0,0]),'pit_reasons':np.array([0,0])}
        before=quality_mask(pool,'PIT_SAFE');pool['quality'][:]=2
        np.testing.assert_array_equal(before,quality_mask(pool,'PIT_SAFE'))

    def test_post_entry_jump_is_diagnostic_not_entry_exclusion(self):
        b=bars(5);b['c'][3:]=300;b['o'][3:]=300;b['h'][3:]=301;b['l'][3:]=299
        f=causal_bar_flags(b,CONFIG)
        self.assertEqual(window_bits(f,[2])[0],0)
        self.assertEqual(window_bits(f,[2],after=2)[0],2)
        pool={'days':np.array([2]),'quality':np.array([1]),'pit_reasons':window_bits(f,[2])}
        self.assertTrue(quality_mask(pool,'PIT_SAFE')[0])
        self.assertFalse(quality_mask(pool,'VALID_ONLY')[0])

    def test_future_reports_cannot_use_valid_as_primary(self):
        config=read(Path(ROOT)/'trader-backtest/event-portfolio-config.json')
        self.assertEqual(config['primary_quality'],'PIT_SAFE')
        config['primary_quality']='VALID_ONLY'
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,'PIT_SAFE must be the primary'):
                full_portfolio_run(Path(tmp)/'absent-base',config,Path(tmp)/'output')

    def test_panel_keeps_rank_and_next_open_exact_horizon(self):
        pool={'days':np.array([0,0,0]),'assets':np.array([30,20,10]),
              'paths':np.ones((3,21)),'open_paths':np.ones((3,20)),
              'quality':np.array([1,0,0]),'pit_reasons':np.array([0,2,0])}
        for variant,expected in [('ALL',[30,20,10]),('PIT_SAFE',[30,10]),('VALID_ONLY',[20,10]),('INVALID_ONLY_PIT',[30,20,10])]:
            p,o,a,i=panel(pool,variant,22,10)
            np.testing.assert_array_equal(a,expected);self.assertTrue(np.all(o[0]==-1))
            book=historical(p,o,a,5,20,10000,.001,open_paths=pool['open_paths'][i])
            used=book[5].astype(bool)
            self.assertTrue(np.all(book[7][used,0]==1))
            self.assertTrue(np.all(book[7][used,1]==20))

    def test_inventory_covers_every_legacy_rule_and_aggregation(self):
        inventory=rule_inventory();bits={r['bit'] for r in inventory if r['bit'] is not None}
        self.assertEqual(bits,{1,2,4,8,16,32,64,128,256,512,1024,2048})
        for bit in [16,32,64,1024]:
            r=next(r for r in inventory if r['bit']==bit)
            self.assertEqual(r['classification'],'RETROSPECTIVE');self.assertFalse(r['used_by_pit_safe'])
        self.assertIn('NOT_POINT_IN_TIME_CERTIFIED',VINTAGE_STATUS)


class PublicationClockTests(unittest.TestCase):
    def records(self):
        return [dict(session='2020-01-02',close_at='2020-01-02T21:00:00Z',available_at='2020-01-02T21:00:01Z',c=100),
                dict(session='2020-01-02',close_at='2020-01-02T21:00:00Z',available_at='2021-01-01T12:00:00Z',c=1)]

    def test_later_vendor_correction_is_not_retroactively_selected(self):
        records=self.records()
        result=asof_revisions(records,'2020-01-02T21:01:00Z')
        self.assertEqual(result[0]['c'],100)
        self.assertEqual(asof_revisions(records[:1],'2020-01-02T21:01:00Z'),result)
        self.assertEqual(asof_revisions(records,'2021-01-02T00:00:00Z')[0]['c'],1)

    def test_future_close_is_unavailable_even_with_bad_early_publication(self):
        row=self.records()[0];row['close_at']='2020-01-03T21:00:00Z'
        self.assertEqual(asof_revisions([row],'2020-01-02T21:01:00Z'),[])

    def test_missing_publication_timestamp_never_inferred(self):
        row=self.records()[0];del row['available_at']
        with self.assertRaisesRegex(ValueError,'publication'):
            asof_revisions([row],'2020-01-02T21:01:00Z')

    def test_snapshot_has_no_fabricated_historical_availability(self):
        with self.assertRaisesRegex(ValueError,'timezone-aware'):
            asof_revisions(self.records(),'2020-01-02T21:01:00')


class AnnotationIntegration(unittest.TestCase):
    def test_raw_snapshot_annotation_ignores_future_actions_and_metadata(self):
        dates=['2020-01-%02d'%d for d in range(2,8)]
        timestamps=[int(dt.datetime.fromisoformat(d+'T14:30:00+00:00').timestamp()) for d in dates]
        raw={'timestamp':timestamps,'meta':{'firstTradeDate':timestamps[0],'currency':'USD','instrumentType':'EQUITY'},
             'indicators':{'quote':[{'open':[100]*6,'high':[101]*6,'low':[99]*6,'close':[100]*6,'volume':[1000]*6}]}}
        pool={'days':np.array([1]),'assets':np.array([0]),'paths':np.ones((1,3)),
              'open_paths':np.ones((1,2)),'quality':np.array([0],dtype=np.uint8)}
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);(base/'analysis').mkdir();(base/'events').mkdir();(base/'data').mkdir()
            write(base/'analysis/matrix-metadata.json',{'days':dates,'universe':['X'],'test_first_index':0})
            write(base/'events/frozen-methodology.json',{'quality':CONFIG,'feature_window_sessions':260})
            def evaluate(doc):
                body=json.dumps({'chart':{'result':[doc]}}).encode();(base/'data/X.json.gz').write_bytes(gzip.compress(body))
                write(base/'data/acquisition.json',{'records':[{'ticker':'X','raw_file':'X.json.gz','raw_sha256':hashlib.sha256(body).hexdigest()}]})
                return annotate_pools({'X':pool},base,2)['X']['pit_reasons']
            before=evaluate(raw)
            raw['events']={'splits':{'future':{'date':timestamps[4],'numerator':1,'denominator':100000}}}
            raw['meta']['firstTradeDate']=timestamps[4];raw['meta']['currency']='EUR'
            raw['indicators']['adjclose']=[{'adjclose':[None]*6}]
            after=evaluate(raw)
            np.testing.assert_array_equal(before,after);self.assertEqual(after[0],0)
            raw['indicators']['quote'][0]['volume'][1]=0
            raw['indicators']['quote'][0]['close'][1]=100.5
            self.assertEqual(evaluate(raw)[0],4)


if __name__=='__main__':unittest.main()
