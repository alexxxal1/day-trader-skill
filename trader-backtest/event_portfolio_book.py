"""Causal event-return book: queued signals, next open, scheduled close.

Opens and closes are mandatory. No stops, interest, leverage or broker payloads.
"""
import math
import numpy as np
try:
    from numba import njit, prange
    ACCELERATOR="numba"
except ImportError:
    ACCELERATOR="python"
    def njit(*args, **kwargs):
        return lambda f:f
    prange=range


@njit(cache=True)
def _book(paths,opens,offers,assets,slots,horizon,initial,side_cost,method,seed,block,record):
    """paths[:,1:] are session 1 through h closes; opens[:,0] is entry.

    Offers are queued one session after the signal. Opening allocations use
    only current opens and cash already realized. Today's scheduled close
    proceeds cannot pay for today's opening entries.
    """
    np.random.seed(seed)
    ndays,n=offers.shape[0],len(paths)
    ids=np.full(slots,-1,np.int64)
    held_assets=np.full(slots,-1,np.int64)
    entries=np.zeros(slots,np.int64)
    allocations=np.zeros(slots)
    entry_value=np.zeros(slots)
    schedule=np.arange(ndays)
    if method==2:
        for dest in range(0,ndays,block):
            start=np.random.randint(0,ndays-block+1)
            for j in range(min(block,ndays-dest)):schedule[dest+j]=start+j
    curve=np.zeros(ndays+1 if record else 0)
    count_curve=np.zeros(ndays if record else 0,np.int32)
    invested=np.zeros(n if record else 0)
    pnl=np.zeros(n if record else 0)
    accepted=np.zeros(n if record else 0,np.uint8)
    # cash begin, equity/gross/count before entries, cash/gross/equity/count
    # after entries; cash/gross/equity/count end, entry outflows, exit inflows,
    # realized P&L, historical cost basis still held.
    daily=np.zeros((ndays if record else 0,16))
    # entry/exit day, batch equity, cash before entry, debit, mid investment,
    # exit mid value, proceeds, target budget, signal day, fraction, fees.
    ledger=np.full((n if record else 0,12),np.nan)
    if record:curve[0]=initial
    cash=peak=phase_peak=min_cash=initial
    maxdd=phase_dd=exposure_sum=count_sum=cost_total=0.0
    max_exposure=max_allocation=0.0
    max_count=closed=wins=admitted=cash_rejections=duplicate_rejections=0
    identity_violations=0
    for d in range(ndays):
        cash_begin=cash
        gross=0.0
        count=0
        for s in range(slots):
            if ids[s]>=0:
                gross+=entry_value[s]*opens[ids[s],d-entries[s]]
                count+=1
        equity_open=cash+gross
        before_gross,before_count=gross,count
        budget=equity_open/slots
        entries_out=0.0
        if d+horizon-1<ndays and count<slots:
            for j in range(offers.shape[1]):
                offer=offers[schedule[d],j]
                if offer<0 or count>=slots:break
                ticker=assets[offer]
                duplicate=False
                for s in range(slots):
                    if ids[s]>=0 and held_assets[s]==ticker:
                        duplicate=True
                        break
                if duplicate:
                    duplicate_rejections+=1
                    continue
                amount=min(budget,max(0.0,cash))
                if amount<=1e-12*max(1.0,equity_open):
                    cash_rejections+=1
                    break
                source=np.random.randint(0,n) if method==1 else offer
                slot=-1
                for s in range(slots):
                    if ids[s]<0:
                        slot=s
                        break
                if slot<0:raise ValueError("no free position slot")
                mid=amount/(1+side_cost)
                if record:
                    ledger[source,0]=d
                    ledger[source,2]=equity_open
                    ledger[source,3]=cash
                    ledger[source,4]=amount
                    ledger[source,5]=mid
                    ledger[source,8]=budget
                    ledger[source,9]=d-1
                    ledger[source,10]=amount/equity_open
                    ledger[source,11]=mid*side_cost
                cash-=amount
                entries_out+=amount
                ids[slot],held_assets[slot],entries[slot]=source,ticker,d
                allocations[slot],entry_value[slot]=amount,mid
                cost_total+=mid*side_cost
                gross+=mid
                count+=1
                admitted+=1
                max_allocation=max(max_allocation,amount/equity_open)
                if record:
                    invested[source]+=amount
                    accepted[source]=1
        open_after=cash+gross
        cash_after_entries=cash
        open_exposure=gross/open_after if open_after>0 else 0.0
        max_exposure=max(max_exposure,open_exposure)
        exposure_sum+=open_exposure
        count_sum+=count
        max_count=max(max_count,count)
        phase_peak=max(phase_peak,equity_open)
        if phase_peak>0:phase_dd=max(phase_dd,1-open_after/phase_peak)
        if record:
            daily[d,0:8]=np.array([cash_begin,equity_open,before_gross,before_count,cash,gross,open_after,count])
        gross_close=0.0
        for s in range(slots):
            if ids[s]>=0:
                gross_close+=entry_value[s]*paths[ids[s],d-entries[s]+1]
                for other in range(s):
                    if ids[other]>=0 and held_assets[other]==held_assets[s]:identity_violations+=1
        close_before=cash+gross_close
        max_exposure=max(max_exposure,gross_close/close_before if close_before>0 else 0.0)
        exits_in=realized=0.0
        for s in range(slots):
            if ids[s]>=0 and d-entries[s]+1==horizon:
                i=ids[s]
                mid=entry_value[s]*paths[i,horizon]
                proceeds=mid*(1-side_cost)
                cash+=proceeds
                exits_in+=proceeds
                cost_total+=mid*side_cost
                result=proceeds-allocations[s]
                realized+=result
                closed+=1
                if result>0:wins+=1
                if record:
                    pnl[i]+=result
                    ledger[i,1]=d
                    ledger[i,6]=mid
                    ledger[i,7]=proceeds
                    ledger[i,11]+=mid*side_cost
                ids[s],held_assets[s]=-1,-1
        gross_end=basis=0.0
        end_count=0
        for s in range(slots):
            if ids[s]>=0:
                gross_end+=entry_value[s]*paths[ids[s],d-entries[s]+1]
                basis+=allocations[s]
                end_count+=1
        equity=cash+gross_end
        min_cash=min(min_cash,cash,cash_after_entries)
        max_exposure=max(max_exposure,gross_end/equity if equity>0 else 0.0)
        peak=max(peak,equity)
        if peak>0:maxdd=max(maxdd,1-equity/peak)
        phase_peak=max(phase_peak,close_before)
        if phase_peak>0:phase_dd=max(phase_dd,1-equity/phase_peak)
        if record:
            curve[d+1]=equity
            count_curve[d]=end_count
            daily[d,8:16]=np.array([cash,gross_end,equity,end_count,entries_out,exits_in,realized,basis])
    values=np.array([equity,maxdd,exposure_sum/ndays,count_sum/ndays,max_count,closed,wins,
        cost_total,admitted,cash_rejections,duplicate_rejections,cash,min_cash,max_exposure,
        identity_violations,max_allocation,phase_dd])
    return values,curve,count_curve,invested,pnl,accepted,daily,ledger


@njit(parallel=True,cache=True)
def _bootstrap(paths,opens,offers,assets,slots,horizon,initial,side_cost,method,seed,block,simulations):
    out=np.zeros((simulations,17))
    for i in prange(simulations):
        values,_,_,_,_,_,_,_=_book(paths,opens,offers,assets,slots,horizon,initial,side_cost,method,
            (seed+i)%2147483647,block,False)
        out[i]=values
    return out


def validate_inputs(paths,open_paths,offers,assets,slots,horizon,initial,side_cost,method,block):
    paths=np.asarray(paths,dtype=np.float64,order="C")
    if open_paths is None:raise ValueError("explicit observed opening prices required; no inferred opens")
    opens=np.asarray(open_paths,dtype=np.float64,order="C")
    offers=np.asarray(offers,dtype=np.int32,order="C")
    assets=np.asarray(assets,dtype=np.int32)
    if paths.ndim!=2 or not len(paths) or paths.shape[1]!=horizon+1 or not np.all(np.isfinite(paths)) or np.any(paths<=0):
        raise ValueError("complete positive observed close paths required")
    if opens.shape!=(len(paths),horizon) or not np.all(np.isfinite(opens)) or np.any(opens<=0):
        raise ValueError("complete positive observed open paths required")
    if not np.allclose(paths[:,0],1,rtol=0,atol=1e-12) or not np.allclose(opens[:,0],1,rtol=0,atol=1e-12):
        raise ValueError("entry-normalized paths must start at one")
    if offers.ndim!=2 or len(offers)<horizon or len(assets)!=len(paths):raise ValueError("invalid dimensions")
    if np.any(offers<-1) or np.any(offers>=len(paths)):raise ValueError("offer references an absent event")
    if type(slots) is not int or slots<1 or type(horizon) is not int or horizon<1:raise ValueError("positive slots/horizon required")
    if not math.isfinite(initial) or initial<=0 or not 0<=side_cost<1:raise ValueError("invalid capital/cost")
    if method not in (0,1,2):raise ValueError("unknown resampling method")
    if method==2 and not 1<=block<=len(offers)//2:raise ValueError("block bootstrap requires two full blocks")
    return paths,opens,offers,assets


def historical(paths,offers,assets,slots,horizon,initial=10000,side_cost=.001,*,open_paths=None):
    paths,opens,offers,assets=validate_inputs(paths,open_paths,offers,assets,slots,horizon,initial,side_cost,0,1)
    return _book(paths,opens,offers,assets,slots,horizon,initial,side_cost,0,0,1,True)


def bootstrap(paths,offers,assets,slots,horizon,initial,side_cost,method,seed,block,simulations,*,open_paths=None):
    method={"IID_EVENT_PATH":1,"SESSION_BLOCK":2}.get(method,0)
    if not method or type(simulations) is not int or simulations<1 or type(seed) is not int or seed<0:raise ValueError("invalid bootstrap configuration")
    paths,opens,offers,assets=validate_inputs(paths,open_paths,offers,assets,slots,horizon,initial,side_cost,method,block)
    result=_bootstrap(paths,opens,offers,assets,slots,horizon,initial,side_cost,method,seed,block,simulations)
    if not np.all(np.isfinite(result)):raise ValueError("nonfinite bootstrap outcome")
    if np.any(result[:,12]<-1e-8) or np.any(result[:,13]>1+1e-10) or np.any(result[:,14]!=0):
        raise AssertionError("cash/exposure/identity invariant violated")
    return result
