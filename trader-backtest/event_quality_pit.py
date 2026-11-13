"""Causal quality decisions; historical data-vintage certification is separate.

The decision kernel accepts OHLCV only. Corporate actions, adjclose, SEC,
current identity metadata and future bars cannot determine admission.
"""
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from common import read
from daily_history import mapped
from data_quality import REASONS

CAUSAL_BITS = 1 | 2 | 4 | 8 | 2048
CAUSAL_INVALID_BITS = 1 | 2048
LEGACY_INVALID_BITS = 1 | 32 | 512 | 2048
VARIANTS = ("ALL", "PIT_SAFE", "VALID_ONLY", "INVALID_ONLY_PIT")
VINTAGE_STATUS = "SNAPSHOT_RECONSTRUCTION_NOT_POINT_IN_TIME_CERTIFIED"


def rule_inventory():
    rows = [
        (1, "INVALID", "OHLCV nonfinite/nonpositive, negative volume or inconsistent OHLC bounds", "OHLCV at that bar", "POINT_IN_TIME_SAFE", "Observable after that bar closes; source vintage unverified"),
        (2, "SUSPECT", "Close / previous close >= 2 or <= 0.5", "Two consecutive closes", "POINT_IN_TIME_SAFE", "Only past/current closes permitted; magnitude does not prove invalidity"),
        (4, "SUSPECT", "Zero reported volume with changed close", "Current volume and two closes", "POINT_IN_TIME_SAFE", "Observable after current close"),
        (8, "SUSPECT", "At least 20 consecutive unchanged-close, zero-volume sessions", "Trailing closes and volume", "POINT_IN_TIME_SAFE", "Trailing count only"),
        (16, "SUSPECT", "Split resembles unreconciled quote units, tolerance 1.25", "Declared splits, quote jumps, action +/- one session", "RETROSPECTIVE", "No archived announcement/availability timestamps; can mark a session before action"),
        (32, "INVALID", "Adjclose confirms mixed quote units at a split", "Declared split, quote and adjclose changes, tolerance 1.25", "RETROSPECTIVE", "Reconciled action and adjusted-price snapshot, not archived contemporaneous evidence"),
        (64, "SUSPECT", "Adjustment-ratio change >1.25 or <0.8 without nearby action", "Adjclose/close and full action/dividend/capital-gain archive +/- one session", "RETROSPECTIVE", "Later action data can remove or add this flag"),
        (128, "SUSPECT", "History more than one session before declared inception", "Current vendor firstTradeDate metadata", "UNKLAR", "No historical metadata/security identity vintage"),
        (256, "SUSPECT", "Source exchange timezone/market mismatch", "Current vendor exchangeTimezoneName", "UNKLAR", "Current metadata need not describe historical listing"),
        (512, "INVALID", "Instrument not EQUITY or currency not USD", "Current vendor instrumentType/currency", "UNKLAR", "No timestamped historical instrument/currency archive"),
        (1024, "SUSPECT", "Supplied adjclose missing/nonfinite/nonpositive", "Current vendor adjclose diagnostic", "RETROSPECTIVE", "Adjclose may incorporate later actions/corrections; not used in PIT_SAFE"),
        (2048, "INVALID", "Expected exchange session absent", "Observed session calendar and received bars through decision close", "POINT_IN_TIME_SAFE", "Known only after that expected close; late data/revisions need publication timestamps"),
    ]
    result=[dict(bit=b,flag=REASONS[b],severity=s,rule=r,data=d,classification=c,availability=a,
                 used_by_pit_safe=bool(b & CAUSAL_BITS)) for b,s,r,d,c,a in rows]
    result += [
        dict(bit=None,flag="INVALID_SPLIT_RATIO",severity="INVALID (shares bit 1)",
             rule="Declared split numerator/denominator nonpositive or nonfinite",data="Vendor action archive",
             classification="RETROSPECTIVE",availability="Excluded from PIT numeric rule; no action publication archive",used_by_pit_safe=False),
        dict(bit=None,flag="LEGACY_WINDOW_AGGREGATION",severity="maximum across bars",
             rule="260 feature sessions including T plus every bar T+1..T+h; VALID iff maximum severity is zero",
             data="Feature bars, future outcome bars, all retrospective annotations",classification="RETROSPECTIVE",
             availability="Outcome bars unavailable at entry; a flag on a future bar removes the entire event",used_by_pit_safe=False),
        dict(bit=None,flag="NO_RULE_FIRES",severity="VALID",rule="No SUSPECT or INVALID flag in the legacy combined window",
             data="All preceding rules and entire outcome window",classification="RETROSPECTIVE",
             availability="Absence of future flags is not knowable at entry",used_by_pit_safe=False),
    ]
    return result


def causal_bar_flags(ohlcv, config):
    """Pure prefix-causal OHLCV kernel. Never reads action or metadata fields."""
    o,h,l,c,v=(np.asarray(ohlcv[k],dtype=float) for k in ("o","h","l","c","v"))
    if any(a.shape!=c.shape for a in (o,h,l,v)) or c.ndim!=1:
        raise ValueError("aligned one-dimensional OHLCV required")
    flags=np.zeros(len(c),dtype=np.uint32)
    present=np.asarray(ohlcv.get("present",np.logical_or.reduce([np.isfinite(x) for x in (o,h,l,c,v)])),dtype=bool)
    numeric=np.logical_and.reduce([np.isfinite(x) for x in (o,h,l,c,v)])
    numeric &= (np.minimum.reduce([o,h,l,c])>0)&(v>=0)&(l<=np.minimum(o,c))&(np.maximum(o,c)<=h)
    flags[present & ~numeric]|=1
    flags[~present]|=2048
    with np.errstate(divide="ignore",invalid="ignore"):
        gross=c/np.r_[np.nan,c[:-1]]
    finite=np.isfinite(gross)
    jump=finite&((gross>=config["daily_gross_jump_factor"])|(gross<=1/config["daily_gross_jump_factor"]))
    flags[jump]|=2
    flags[(v==0)&finite&(gross!=1)]|=4
    stale=np.isfinite(c)&(v==0)&finite&(gross==1)
    count=np.arange(len(c))-np.maximum.accumulate(np.where(stale,-1,np.arange(len(c))))
    flags[count>=config["stale_zero_volume_sessions"]]|=8
    return flags


def window_bits(flags,signal_days,lookback=260,after=0):
    """Inclusive signal day, fixed trailing window; after is diagnostic only."""
    if lookback<1 or after<0:raise ValueError("invalid quality window")
    return np.array([np.bitwise_or.reduce(flags[max(0,int(t)-lookback+1):int(t)+after+1],initial=np.uint32(0))
                     for t in signal_days],dtype=np.uint32)


def quality_mask(pool,name):
    n=len(pool["days"])
    if name=="ALL":return np.ones(n,dtype=bool)
    if name=="VALID_ONLY":return np.asarray(pool["quality"])==0
    if name not in ("PIT_SAFE","INVALID_ONLY_PIT"):raise ValueError("unknown data-quality panel")
    if "pit_reasons" not in pool:raise ValueError("PIT quality annotations required; no VALID fallback")
    bits=np.asarray(pool["pit_reasons"],dtype=np.uint32)
    mask=CAUSAL_BITS if name=="PIT_SAFE" else CAUSAL_INVALID_BITS
    return (bits&mask)==0


def asof_revisions(revisions,asof):
    """Archive adapter: select only revisions actually published by asof.

    No publication timestamp is inferred from the trading date. The current
    downloaded snapshot lacks such archives and does NOT use this adapter.
    """
    def stamp(x):
        t=dt.datetime.fromisoformat(x.replace("Z","+00:00"))
        if t.tzinfo is None:raise ValueError("timezone-aware availability timestamps required")
        return t
    cutoff=stamp(asof);selected={}
    for row in revisions:
        if not row.get("available_at") or not row.get("close_at"):
            raise ValueError("archived publication and bar close timestamps required")
        available,close=stamp(row["available_at"]),stamp(row["close_at"])
        if available>cutoff or close>cutoff:continue
        old=selected.get(row["session"])
        if old is None or available>stamp(old["available_at"]):selected[row["session"]]=dict(row)
    return [selected[k] for k in sorted(selected)]


def quote_arrays(result,days):
    """Raw quote columns only; no normalization using future action metadata."""
    mapping={d:i for i,d in enumerate(days)}
    local=pd.to_datetime(result["timestamp"],unit="s",utc=True).tz_convert("America/New_York").strftime("%Y-%m-%d")
    pos=np.array([mapping.get(d,-1) for d in local])
    present=np.zeros(len(days),dtype=bool);present[pos[pos>=0]]=True
    out={"present":present}
    for k,name in (("o","open"),("h","high"),("l","low"),("c","close"),("v","volume")):
        col=np.asarray(result["indicators"]["quote"][0].get(name) or [],dtype=float)
        take=(pos>=0)&(np.arange(len(pos))<len(col));v=np.full(len(days),np.nan)
        v[pos[take]]=col[np.flatnonzero(take)];out[k]=v
    return out


def annotate_pools(pools,base,horizon=20,include_legacy=False):
    """Annotate existing event/rank pools, preserving every price and order.

    All PIT decisions end at signal close T. No price after T, action table,
    adjclose, source identity metadata or legacy reason determines that mask.
    """
    base=Path(base);meta=read(base/"analysis/matrix-metadata.json")
    method=read(base/"events/frozen-methodology.json")
    records={r["ticker"]:r for r in read(base/"data/acquisition.json")["records"]}
    output={name:dict(pool) for name,pool in pools.items()}
    for pool in output.values():
        pool["pit_reasons"]=np.zeros(len(pool["days"]),dtype=np.uint32)
        if include_legacy:
            pool["legacy_feature_reasons"]=np.zeros(len(pool["days"]),dtype=np.uint32)
            pool["legacy_outcome_reasons"]=np.zeros(len(pool["days"]),dtype=np.uint32)
    legacy=mapped(base/"events/quality-reasons.u32",meta["shape"],"uint32") if include_legacy else None
    assets=np.unique(np.concatenate([p["assets"] for p in pools.values()]))
    for j,asset in enumerate(assets):
        sym=meta["universe"][asset];record=records[sym]
        body=gzip.decompress((base/"data"/record["raw_file"]).read_bytes())
        if hashlib.sha256(body).hexdigest()!=record["raw_sha256"]:raise ValueError("raw snapshot changed: "+sym)
        result=json.loads(body)["chart"]["result"][0]
        flags=causal_bar_flags(quote_arrays(result,meta["days"]),method["quality"])
        for pool in output.values():
            chosen=np.flatnonzero(pool["assets"]==asset)
            if not len(chosen):continue
            days=pool["days"][chosen]+meta["test_first_index"]
            pool["pit_reasons"][chosen]=window_bits(flags,days,method["feature_window_sessions"])
            if include_legacy:
                r=np.asarray(legacy[:,asset])
                pool["legacy_feature_reasons"][chosen]=window_bits(r,days,method["feature_window_sessions"])
                pool["legacy_outcome_reasons"][chosen]=np.array([np.bitwise_or.reduce(r[t+1:t+horizon+1],initial=np.uint32(0)) for t in days])
        if (j+1)%1000==0:print("Causal quality annotation",j+1,"/",len(assets),flush=True)
    if include_legacy:
        for pool in output.values():
            r=pool["legacy_feature_reasons"]|pool["legacy_outcome_reasons"]
            status=np.where(r&LEGACY_INVALID_BITS,2,np.where(r,1,0))
            if not np.array_equal(status,pool["quality"]):raise AssertionError("legacy rule reconstruction disagrees with frozen VALID")
    return output
