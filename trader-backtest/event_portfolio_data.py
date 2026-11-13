"""Read fixed native events and observed forward trajectories, without new rules."""
from pathlib import Path

import numpy as np

from common import digest, read, write
from daily_history import mapped
from event_signals import BITS, NAMES
from event_quality_pit import quality_mask


def catalog(analysis,events,output,strategy,horizon):
    analysis,events,output=Path(analysis),Path(events),Path(output)
    meta=read(analysis/"matrix-metadata.json")
    emeta=read(events/"event-metadata.json")
    if any(meta[k]!=emeta[k] for k in ("shape","universe","days","acquisition_sha256")):
        raise ValueError("native event/price matrices do not match")
    bit=BITS[NAMES.index(strategy)]
    path=output/("pool-%s-%d.npz"%(strategy,horizon))
    provenance={"execution_schema":"NEXT_OPEN_V2","analysis":digest(meta),"event":digest(emeta),"strategy":strategy,"horizon":horizon,
        "methodology":digest(read(events/"frozen-methodology.json"))}
    if path.exists():
        if read(path.with_suffix(".json"))["provenance"]!=provenance:
            raise ValueError("event-pool cache provenance differs")
        with np.load(path) as z:
            return {k:z[k] for k in z.files},read(path.with_suffix(".json"))["counts"]
    shape=meta["shape"]
    matrix=mapped(events/"events-COOLDOWN20.u8",shape,"uint8")
    close=mapped(analysis/"close.f64",shape,"float64")
    after=mapped(analysis/"forward-contiguous.u8",shape,"uint8")
    ranks=mapped(analysis/"ranks.f64",(4,)+tuple(shape),"float64")
    quality=mapped(events/("quality-window-%d.u8"%horizon),shape,"uint8")
    days,assets=np.nonzero(matrix&bit)
    keep=(days>=meta["test_first_index"])&(days<=meta["test_last_index"])
    days,assets=days[keep],assets[keep]
    total=len(days)
    within=days+horizon<=meta["test_last_index"]
    right_censored=int(np.count_nonzero(~within))
    days,assets=days[within],assets[within]
    complete=after[days,assets]>=horizon
    missing=int(np.count_nonzero(~complete))
    days,assets=days[complete],assets[complete]
    trajectories=np.asarray(close[days[:,None]+np.arange(horizon+1),assets[:,None]],dtype=float)
    numeric=np.all(np.isfinite(trajectories)&(trajectories>0),axis=1)
    missing+=int(np.count_nonzero(~numeric))
    days,assets,trajectories=days[numeric],assets[numeric],trajectories[numeric]
    rs=np.asarray(ranks[3,days,assets])
    rs=np.where(np.isfinite(rs),rs,-np.inf)
    liquid=np.zeros(len(days))
    open_quotes=np.full((len(days),horizon),np.nan)
    for i in np.unique(assets):
        selected=np.flatnonzero(assets==i)
        with np.load(analysis/"stock-cache"/(meta["universe"][i]+".npz")) as z:
            dollar=z["c"]*z["v"]
            observed_opens=z["o"]
        open_quotes[selected]=observed_opens[days[selected,None]+np.arange(1,horizon+1)]
        for j in selected:
            sample=dollar[max(0,days[j]-19):days[j]+1]
            liquid[j]=float(np.mean(sample)) if np.all(np.isfinite(sample)) else 0
    complete_open=np.all(np.isfinite(open_quotes)&(open_quotes>0),axis=1)
    missing_opens=int(np.count_nonzero(~complete_open))
    missing+=missing_opens
    days,assets,trajectories,open_quotes,rs,liquid=(v[complete_open] for v in (days,assets,trajectories,open_quotes,rs,liquid))
    entry_open=open_quotes[:,0].copy()
    signal_close=trajectories[:,0].copy()
    exit_close=trajectories[:,-1].copy()
    trajectories=np.c_[np.ones(len(days)),trajectories[:,1:]/entry_open[:,None]]
    open_quotes=open_quotes/entry_open[:,None]
    # Native status is identical within each separate stream. RS then
    # liquidity then the alphabetically sorted native universe is fixed.
    order=np.lexsort((assets,-liquid,-rs,days))
    result={"days":(days[order]-meta["test_first_index"]).astype("int32"),
        "assets":assets[order].astype("int32"),"paths":trajectories[order],
        "open_paths":open_quotes[order],"entry_open":entry_open[order],"exit_close":exit_close[order],
        "signal_close":signal_close[order],
        "RS_PROXY":rs[order],"liquidity":liquid[order],"quality":np.asarray(quality[days[order],assets[order]])}
    counts={"native_cooldown_events":total,"right_censored":right_censored,"missing_price_windows":missing,
        "missing_entry_or_mark_opens":missing_opens,
        "computable":len(days),"quality_counts":{label:int(np.count_nonzero(result["quality"]==i)) for i,label in enumerate(("VALID","SUSPECT","INVALID"))}}
    output.mkdir(parents=True,exist_ok=True)
    np.savez_compressed(path,**result)
    write(path.with_suffix(".json"),{"provenance":provenance,"counts":counts,
        "meaning":"Signal close T, observed open T+1 entry; h observed sessions through close T+h. Price-return execution proxy, not stop-based trades"})
    return result,counts


def panel(pool,name,ndays,max_slots=10):
    indices=np.flatnonzero(quality_mask(pool,name))
    paths=np.ascontiguousarray(pool["paths"][indices])
    assets=np.ascontiguousarray(pool["assets"][indices],dtype="int32")
    dates=pool["days"][indices]+1
    # At most max_slots distinct ticker identities can be excluded as already
    # active. The first 2*max_slots ranked offers suffice for every slot count
    # <=max_slots, without outcome-dependent truncation of the competition.
    offers=np.full((ndays,2*max_slots),-1,dtype="int32")
    per_day=np.zeros(ndays,dtype=int)
    for i,day in enumerate(dates):
        if per_day[day]<offers.shape[1]:
            offers[day,per_day[day]]=i
            per_day[day]+=1
    return paths,offers,assets,indices
