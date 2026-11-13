#!/usr/bin/env python3
"""Execution/accounting audit, matched benchmarks and outcome-tail diagnostics.

All inputs are frozen local quotes. No strategy rules, ranks or raw prices are
changed. Tail variants are explicitly nondeployable sensitivity index paths.
"""
import argparse
import csv
import datetime as dt
import gzip
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from common import digest,read,write
from daily_history import normalize
from data_quality import classify,REASONS
from event_portfolio_book import historical
from event_portfolio_data import panel
from event_portfolio_run import history_metrics
from event_signals import NAMES
from portfolio_metrics import drawdowns
from run import fingerprints


def load_pool(folder,strategy,horizon=20):
    with np.load(Path(folder)/"catalog"/("pool-%s-%d.npz"%(strategy,horizon))) as z:
        return {k:z[k] for k in z.files}


def validate_book(book,pool,indices,slots,horizon,calendar,meta):
    values,curve,counts,invested,pnl,accepted,daily,ledger=book
    used=np.flatnonzero(accepted)
    assert values[14]==0 and values[4]<=slots and values[13]<=1+1e-10 and values[12]>=0
    np.testing.assert_allclose(daily[:,1],daily[:,0]+daily[:,2],rtol=1e-12,atol=1e-8)
    np.testing.assert_allclose(daily[:,6],daily[:,4]+daily[:,5],rtol=1e-12,atol=1e-8)
    np.testing.assert_allclose(daily[:,4],daily[:,0]-daily[:,12],rtol=1e-12,atol=1e-8)
    np.testing.assert_allclose(daily[:,8],daily[:,4]+daily[:,13],rtol=1e-12,atol=1e-8)
    np.testing.assert_allclose(daily[:,10],daily[:,8]+daily[:,9],rtol=1e-12,atol=1e-8)
    np.testing.assert_allclose(daily[1:,0],daily[:-1,8],rtol=0,atol=0)
    np.testing.assert_allclose(daily[:,10],curve[1:],rtol=0,atol=0)
    assert np.all(daily[:,4]>=0) and np.all(daily[:,8]>=0)
    assert np.all(daily[:,7]<=slots) and np.all(daily[:,11]<=slots)
    assert math.isclose(float(pnl.sum()),curve[-1]-curve[0],rel_tol=1e-9,abs_tol=1e-6)
    assert math.isclose(float(np.log(curve[1:]/curve[:-1]).sum()),math.log(curve[-1]/curve[0]),abs_tol=1e-10)
    np.testing.assert_allclose(ledger[used,1]-ledger[used,0]+1,horizon,rtol=0,atol=0)
    np.testing.assert_allclose(ledger[used,0],pool["days"][indices[used]]+1,rtol=0,atol=0)
    np.testing.assert_allclose(ledger[used,9],pool["days"][indices[used]],rtol=0,atol=0)
    assert np.all(ledger[used,4]<=ledger[used,3]+1e-8)
    assert np.all(ledger[used,10]<=1/slots+1e-12)
    rows=[]
    days=meta["days"][meta["test_first_index"]:meta["test_last_index"]+1]
    for local in used:
        raw=int(indices[local]);entry=int(ledger[local,0]);exit_day=int(ledger[local,1]);signal=int(pool["days"][raw])
        st=dt.datetime.fromisoformat(calendar[days[signal]]["close_utc"].replace("Z","+00:00"))
        et=dt.datetime.fromisoformat(calendar[days[entry]]["open_utc"].replace("Z","+00:00"))
        assert et>st
        rows.append({"event_index":raw,"ticker":meta["universe"][int(pool["assets"][raw])],
            "signal_day":days[signal],"signal_close_utc":calendar[days[signal]]["close_utc"],
            "entry_day":days[entry],"entry_open_utc":calendar[days[entry]]["open_utc"],
            "exit_day":days[exit_day],"exit_close_utc":calendar[days[exit_day]]["close_utc"],
            "holding_sessions":exit_day-entry+1,"entry_equity":float(ledger[local,2]),
            "cash_before_entry":float(ledger[local,3]),"cash_debit":float(ledger[local,4]),
            "entry_mid_value":float(ledger[local,5]),"net_exit_proceeds":float(ledger[local,7]),
            "target_cash_budget":float(ledger[local,8]),"allocation_fraction":float(ledger[local,10]),
            "entry_open_split_adjusted":float(pool["entry_open"][raw]),"exit_close_split_adjusted":float(pool["exit_close"][raw]),
            "net_pnl_usd":float(pnl[local]),"observed_gross_event_return_pct":float((pool["paths"][raw,-1]-1)*100)})
    grouped={}
    for row in rows:grouped.setdefault(row["ticker"],[]).append(row)
    for ticker,positions in grouped.items():
        positions.sort(key=lambda r:r["entry_day"])
        for a,b in zip(positions,positions[1:]):assert b["entry_day"]>a["exit_day"],ticker
    clipped=ledger[used,4]<ledger[used,8]-np.maximum(1e-8,ledger[used,8]*1e-10)
    return rows,{"status":"PASSED","positions_checked":len(used),"minimum_cash":float(values[12]),
        "maximum_exposure":float(values[13]),"duplicate_active_positions":int(values[14]),
        "target_cash_fraction":1/slots,"maximum_actual_cash_fraction":float(values[15]),
        "minimum_actual_cash_fraction":float(np.min(ledger[used,10])) if len(used) else None,
        "cash_limited_entries":int(clipped.sum()),"full_target_entries":int(len(used)-clipped.sum()),
        "interest_credited":0,"daily_close_maxdd":float(values[1]),"open_close_phase_maxdd":float(values[16])}


def tail_variant(pool,kind):
    r=pool["paths"][:,-1]-1
    if kind=="FULL":return pool,{"kind":kind,"changed_events":0}
    result={k:v.copy() for k,v in pool.items()}
    if kind=="REMOVE_TOP_0.1_PERCENT":
        k=max(1,int(np.ceil(len(r)*.001)))
        top=np.argsort(r,kind="stable")[-k:]
        keep=np.ones(len(r),dtype=bool);keep[top]=False
        result={key:v[keep] for key,v in result.items()}
        return result,{"kind":kind,"changed_events":k,"cohort_events":len(r),
            "smallest_removed_gross_return_pct":float(r[top].min()*100),
            "note":"Outcome-conditioned diagnostic only; remaining native ranking unchanged, no strategy optimization"}
    q={"WINSOR_P99.9":.999,"WINSOR_P99":.99}[kind]
    cap=float(np.quantile(r,q))
    if cap<=0:raise ValueError("positive-tail index scaling requires a positive cutoff")
    changed=r>cap
    alpha=cap/r[changed]
    for key in ("paths","open_paths"):
        result[key][changed]=1+alpha[:,None]*(result[key][changed]-1)
    return result,{"kind":kind,"changed_events":int(changed.sum()),"cohort_events":len(r),
        "gross_return_cap_pct":cap*100,"raw_quotes_changed":False,
        "note":"Derived return-index sensitivity: p'=1+(cap/r)*(p-1), across the whole open/close trajectory. Not historical quotes or a tradable strategy."}


def benchmark(raw,meta,start,side_cost=.001):
    quotes=read(raw) if not str(raw).endswith(".gz") else json.loads(gzip.decompress(Path(raw).read_bytes()))
    result=quotes["chart"]["result"][0]
    values,_=normalize(result,meta["days"])
    valid=np.isfinite(values["o"])&(values["o"]>0)&np.isfinite(values["c"])&(values["c"]>0)
    first=int(np.flatnonzero(valid)[0])
    if start<first:return {"status":"UNAVAILABLE_BEFORE_INCEPTION","first_available":meta["days"][first]},None,first
    last=meta["test_last_index"]
    if not np.all(valid[start:last+1]):return {"status":"BENCHMARK_PRICE_GAPS"},None,first
    curve=np.r_[10000,10000/(1+side_cost)*values["c"][start:last+1]/values["o"][start]]
    curve[-1]*=1-side_cost
    years=(dt.date.fromisoformat(meta["days"][last])-dt.date.fromisoformat(meta["days"][start])).days/365.2425
    cagr=(curve[-1]/10000)**(1/years)-1
    dd=drawdowns(curve.tolist())["maximum"]
    return {"status":"AVAILABLE","start":meta["days"][start],"end":meta["days"][last],
        "entry":"OBSERVED_START_SESSION_OPEN","exit":"END_SESSION_CLOSE","end_capital":float(curve[-1]),
        "cagr":float(cagr),"max_drawdown":dd,"cagr_over_max_drawdown":float(cagr/dd) if dd else None,
        "dividends":False,"side_cost":side_cost},curve,first


def write_rows(path,rows):
    if not rows:return
    with Path(path).open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)


def render(report):
    pct=lambda x:"n/v" if x is None else "%.2f%%"%(100*x)
    lines=["# Abschliessender EVENT_RETURN_PORTFOLIO-Audit","",
        "## Ergebnis des Execution-Audits","",
        "**Fehler bestaetigt:** Das alte Modell verwendete Close T als Entry, obwohl das Signal erst mit Close T feststeht. Diese Ergebnisse sind rechnerisch reproduzierbare Close-Return-Indizes, aber fuer die geforderte kausale Execution nicht gueltig. Der korrigierte Entry ist der beobachtete Open T+1. Strategieparameter, Signale, Ranking und QC-Regeln bleiben unveraendert.","",
        "T+1 ist Handelstag 1. Bei 20 Sessions erfolgt der Verkauf zum Close T+20. Morgens werden nur aktuelle Opens und vorher vorhandenes Cash verwendet; Close-Kurse und Close-Verkaufserloese desselben Tages koennen keine Opening-Position finanzieren. Primärer MaxDD bleibt zum Tages-Close einschliesslich Startkapital gemessen; ein zusaetzlicher Open/Close-Phasen-MaxDD wird separat ausgegeben.","",
        "**Verbleibende Grenze:** Der VALID-/vollstaendige-Outcome-Filter ist weiterhin rueckblickend. Die Execution-Korrektur macht daraus keine vollstaendig Point-in-Time-faehige Strategie. Survivorship Bias, revidierte Vendor-Preise und fehlende historische Identitaeten bleiben bestehen.","",
        "## Vorher / nachher: 20 Sessions, VALID, REALISTIC, 10.000 USD","",
        "| Strategie | Slots | Kapital alt | Kapital korrigiert | CAGR alt | CAGR korrigiert | MaxDD alt | MaxDD korrigiert |",
        "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in report["comparison_before_after"]:
        a,b=row["old"],row["corrected"]
        lines.append("| %s | %d | %.2f | %.2f | %s | %s | %s | %s |"%(row["strategy"],row["slots"],a["end_capital"],b["end_capital"],pct(a["cagr"]),pct(b["cagr"]),pct(a["max_drawdown"]),pct(b["max_drawdown"])))
    lines += ["","## Kapitalallokation und Cash","",
        "Ziel-Cashbudget je neuem Event: aktuelle am Open markierte Equity / Slots, also 20% oder 10%. Das Budget enthaelt Einstiegskosten und ist durch bereits vorhandenes Cash begrenzt. Bei REALISTIC entsprechen die Mid-Marktwerte ohne Cash-Begrenzung 20%/1.001 = 19.98002% bzw. 10%/1.001 = 9.99001% der Pre-Entry-Equity. Es gibt keine Verzinsung, Kredite oder Verwendung unrealisierter Gewinne als Cash.","",
        "Unrealisierte Gewinne gehoeren zur markierten Equity fuer die Zielgroesse; sie werden nicht zu Cash umgebucht. Cash kann nur durch tatsaechliche modellierte Exits steigen. Historische Einstandskosten koennen nach Verlusten ueber 100% der aktuellen Equity liegen; oekonomisches Exposure ist der aktuelle Marktwert / Equity.","",
        "| Strategie | Slots | Entries geprueft | Cash-begrenzte Entries | Minimum Cash | Max Exposure | Max Entry-Cash/Equity | doppelte aktive Ticker |",
        "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in report["accounting"]:
        lines.append("| %s | %d | %d | %d | %.8f | %s | %s | %d |"%(r["strategy"],r["slots"],r["positions_checked"],r["cash_limited_entries"],r["minimum_cash"],pct(r["maximum_exposure"]),pct(r["maximum_actual_cash_fraction"]),r["duplicate_active_positions"]))
    lines += ["","### Konkrete Confluence-Portfoliotage","",
        "Investiert ist hier der aktuelle Marktwert. OPEN_AFTER_ENTRIES ist nach den morgendlichen Entries; CLOSE_AFTER_EXITS nach den spaeteren Exits. Vollstaendige Cash- und Positions-Ledger liegen als CSV vor.","",
        "| Slots | Datum | Phase | Equity USD | Cash USD | offene Positionen | investierter Marktwert USD | Gesamt-Exposure |",
        "|---|---|---|---:|---:|---:|---:|---:|"]
    for r in report["samples"]:
        if r["strategy"]=="CONFLUENCE_SETUP_READY":
            lines.append("| %d | %s | %s | %.2f | %.2f | %d | %.2f | %s |"%(r["slots"],r["date"],r["phase"],r["equity"],r["cash"],r["open_positions"],r["invested_market_value"],pct(r["exposure"])))
    lines += ["","## Corporate Actions und Datenqualitaet","",
        "Die Open- und Close-Quotes stehen auf derselben Yahoo-Split-adjustierten Basis; fraktionale Indexeinheiten erhalten die oekonomischen Renditen ueber korrekt adjustierte Splits. Das ist keine Behauptung, heutige revidierte Quotes seien archivierte damalige Nominal-Fills. Fuer jeden Top-P&L-Fall werden Entry/Exit erneut gegen die eingefrorenen Rohquotes und deren Hash geprueft.","",
        "Anzahl vorhandener Bars und generische Labels: "+json.dumps(report["source_quality"],sort_keys=True),"",
        "Reverse Splits, unerklärte Spruenge, Quote/Adjclose-Brueche, Inception- und Quellen-Mismatches werden weiterhin mit den bestehenden Regeln geprueft. Delisting-/Merger-/Ticker-Reuse-Identitaeten sind ohne vollstaendiges historisches Security-Master-Archiv nicht zertifizierbar. VALID bedeutet bestanden nach diesen Pruefregeln, nicht lueckenlose Identitaetsverifikation. Kein Gewinner und kein PPCB-Fall wurde manuell entfernt.","",
        "### Event-Qualitaet, 20 Sessions","","| Strategie | VALID | SUSPECT | INVALID (rechenbar) | fehlende Preisfenster | rechtszensiert |","|---|---:|---:|---:|---:|---:|"]
    for r in report["event_quality_20"]:
        if r["horizon"]==20:
            q=r["quality_counts"]
            lines.append("| %s | %d | %d | %d | %d | %d |"%(r["strategy"],q["VALID"],q["SUSPECT"],q["INVALID"],r["missing_price_windows"],r["right_censored"]))
    lines += ["","## Confluence: groesste P&L-Beitraege","",
        "Anteile beziehen sich auf das gesamte Netto-P&L des unveraenderten korrigierten Portfolios. Ueber 100% ist bei gegenlaeufigen Verlusten moeglich und ist kein Entfernen-/Reinvestitions-Gegenfaktum.","",
        "| Slots | Gruppe | Positionen | P&L USD | Anteil Netto-P&L | Anteil positives P&L |","|---|---|---:|---:|---:|---:|"]
    for slots,row in report["top_pnl"].items():
        for name,a in row["attribution"].items():
            lines.append("| %s | %s | %d | %.2f | %s | %s |"%(slots,name,a["positions"],a["pnl_usd"],pct(a["share_of_total_net_pnl"]),pct(a["share_of_gross_positive_pnl"])))
    for slots,row in report["top_pnl"].items():
        lines += ["","### Top 20 bei %s Slots"%slots,"","| Ticker | Signal | Entry Open | Exit Close | P&L USD | Return brutto | QC | automatische Flags |","|---|---|---|---|---:|---:|---|---|"]
        for r in row["top_20"]:
            q=r["automated_quality_audit"]
            lines.append("| %s | %s | %s | %s | %.2f | %.2f%% | %s | %s |"%(r["ticker"],r["signal_day"],r["entry_day"],r["exit_day"],r["net_pnl_usd"],r["observed_gross_event_return_pct"],q["label"],", ".join(q["flags"]) or "keine lokalen Flags; historischer Identity-Archiv-Vorbehalt"))
    lines += ["","## Tail-Sensitivitaet: keine neue Strategie","",
        "Cutoffs aus allen korrigierten VALID-Confluence-Event-Returns, identisch fuer 5/10 Slots. Entfernen des obersten 0.1%-Ranks bzw. Winsorisierung bei P99.9/P99 ist ausschliesslich eine rueckblickende Outcome-Sensitivitaet. Bei Winsorisierung wird der gesamte kumulative Return-Index mit p'=1+(cap/r)*(p-1) skaliert. Die Rohpreise und primaeren Ergebnisse bleiben unveraendert; diese abgeleiteten Pfade sind keine historischen Quotes.","",
        "| Slots | Variante | geaenderte Pool-Events | Endkapital USD | CAGR | MaxDD | zugelassene Events |","|---|---|---:|---:|---:|---:|---:|"]
    for r in report["sensitivity"]:
        m=r["metrics"]
        lines.append("| %d | %s | %d | %.2f | %s | %s | %d |"%(r["slots"],r["kind"],r["transform"]["changed_events"],m["end_capital"],pct(m["cagr"]),pct(m["max_drawdown"]),m["admitted_events"]))
    lines += ["","## Benchmarks: exakt passende Zeitraeume","",
        "Preisrenditen ohne Dividenden, 10.000 USD, gleicher BPS-Kostenproxy, Buy-and-hold vom ersten Session-Open bis zum letzten Close. QQQ wird vor seiner verfuegbaren Historie nicht synthetisiert. Fuer den QQQ-Vergleich starten alle Event-Portfolios mit frischem gleichen Kapital im identischen gemeinsamen Zeitraum; native Event-/Cooldown-Zustaende werden nicht neu erfunden.","",
        "| Benchmark | Zeitraum | CAGR | MaxDD | CAGR/MaxDD | Endkapital USD | Status |","|---|---|---:|---:|---:|---:|---|"]
    for name,b in report["benchmarks"].items():
        if b["status"]=="AVAILABLE":
            lines.append("| %s | %s bis %s | %s | %s | %.4f | %.2f | %s |"%(name,b["start"],b["end"],pct(b["cagr"]),pct(b["max_drawdown"]),b["cagr_over_max_drawdown"],b["end_capital"],b["status"]))
        else:lines.append("| %s | ab %s | n/v | n/v | n/v | n/v | %s |"%(name,b.get("first_available","unbekannt"),b["status"]))
    lines += ["","| Strategie | Slots | QQQ-gemeinsamer Zeitraum | CAGR | MaxDD | CAGR/MaxDD | Endkapital USD |","|---|---:|---|---:|---:|---:|---:|"]
    for r in report["matched_qqq_portfolios"]:
        m=r["metrics"]
        lines.append("| %s | %d | %s bis %s | %s | %s | %.4f | %.2f |"%(r["strategy"],r["slots"],r["start"],r["end"],pct(m["cagr"]),pct(m["max_drawdown"]),m["cagr_over_max_drawdown"],m["end_capital"]))
    lines += ["","Die alten hohen Werte duerfen nicht als kausal ausgefuehrte Portfoliowerte weiterverwendet werden. Der Vorher-/Nachher-Vergleich zeigt den Effekt der Execution-Korrektur. Accounting-, Zeithorizont- und Datenpruefungen beseitigen nicht die verbleibenden Stichproben-/PIT-/Identitaetsgrenzen. Keine Aussage ueber Ueberlegenheit wird erzwungen.","",
        "Keine Strategieparameteroptimierung, Orders, Broker-Aufrufe, Commits oder Pushes.",""]
    return "\n".join(lines)


def run(base,corrected,output):
    base,corrected,output=Path(base),Path(corrected),Path(output)
    output.mkdir(parents=True,exist_ok=True)
    before=fingerprints()
    meta=read(base/"analysis/matrix-metadata.json")
    calendar=read(base/"data/calendar.json")["sessions"]
    days=meta["days"][meta["test_first_index"]:meta["test_last_index"]+1]
    old,new=read(base/"results/report.json"),read(corrected/"report.json")
    report={"execution_error_found":"SIGNAL_CLOSE_T_FILLED_AT_SAME_CLOSE_T","corrected_execution":"OPEN_T_PLUS_1_TO_CLOSE_T_PLUS_H",
        "holding_sessions":"Entry day is session 1; for h=20 exit at close T+20",
        "cash_policy":"No interest or borrowing. Only cash can fund entries; same-day close proceeds unavailable at that morning's open",
        "quality_caveat":"VALID/complete-outcome cohort is still retrospective and not point-in-time deployable; QC/ranking/strategy parameters unchanged",
        "accounting":[],"comparison_before_after":[],"samples":[],"top_pnl":{},"sensitivity":[],"benchmarks":{},"matched_qqq_portfolios":[]}
    qc=read(base/"events/quality-ticker-audit.json")
    report["source_quality"]={"VALID":sum(r.get("valid_bars",0) for r in qc),
        "SUSPECT":sum(r.get("suspect_bars",0) for r in qc),"INVALID_PRESENT":sum(r.get("invalid_present_bars",0) for r in qc),
        "missing_sessions_separate":sum(r.get("missing_sessions",0) for r in qc),
        "declared_reverse_splits":sum(r.get("reverse_split_count",0) for r in qc),
        "identity_merger_delisting_archive":"INCOMPLETE_NOT_CERTIFIED_BY_VALID_LABEL"}
    report["event_quality_20"]=new["event_counts"]
    records={r["ticker"]:r for r in read(base/"data/acquisition.json")["records"]}
    quality_method=read(base/"events/frozen-methodology.json")["quality"]
    raw_cache={}
    def case_quality(position):
        sym=position["ticker"]
        if sym not in raw_cache:
            body=gzip.decompress((base/"data"/records[sym]["raw_file"]).read_bytes())
            assert hashlib.sha256(body).hexdigest()==records[sym]["raw_sha256"]
            raw=json.loads(body)["chart"]["result"][0]
            state,reasons,present,actions=classify(raw,meta["days"],quality_method)
            prices,_=normalize(raw,meta["days"])
            raw_cache[sym]=(state,reasons,actions,prices)
        state,reasons,actions,prices=raw_cache[sym]
        signal=meta["days"].index(position["signal_day"]);entry=signal+1;exit_day=meta["days"].index(position["exit_day"])
        a=max(0,signal-259);mask=int(np.bitwise_or.reduce(reasons[a:exit_day+1]))
        assert math.isclose(prices["o"][entry],position["entry_open_split_adjusted"],rel_tol=1e-12)
        assert math.isclose(prices["c"][exit_day],position["exit_close_split_adjusted"],rel_tol=1e-12)
        return {"label":("VALID","SUSPECT","INVALID")[int(np.max(state[a:exit_day+1]))],
            "flags":[name for bit,name in REASONS.items() if mask&bit],
            "declared_actions_in_holding_window":[{k:v for k,v in act.items() if k in ("action","action_type","session","ratio","amount")} for act in actions if entry<=act["session_index"]<=exit_day],
            "entry_exit_rechecked_against_frozen_raw":True,"historical_identity_archive":"MISSING_GLOBAL_LIMITATION"}
    for strategy in NAMES:
        pool=load_pool(corrected,strategy)
        paths,offers,assets,indices=panel(pool,"VALID_ONLY",len(days),10)
        op=pool["open_paths"][indices]
        for slots in (5,10):
            key="%s-h20-VALID_ONLY-s%d-REALISTIC"%(strategy,slots)
            with np.load(corrected/("book-"+key+".npz")) as z:
                book=tuple(z[k] for k in ("values","equity","positions","invested","pnl","accepted","daily","ledger"))
            positions,checks=validate_book(book,pool,indices,slots,20,calendar,meta)
            report["accounting"].append(dict(checks,strategy=strategy,slots=slots))
            write_rows(output/("positions-%s-s%d.csv"%(strategy,slots)),positions)
            prev=next(r["metrics"] for r in old["portfolios"] if r["strategy"]==strategy and r["slots"]==slots and r["horizon"]==20 and r["quality"]=="VALID_ONLY" and r["costs"]=="REALISTIC")
            current=next(r["metrics"] for r in new["portfolios"] if r["strategy"]==strategy and r["slots"]==slots and r["horizon"]==20 and r["quality"]=="VALID_ONLY" and r["costs"]=="REALISTIC")
            report["comparison_before_after"].append({"strategy":strategy,"slots":slots,"old":prev,"corrected":current})
            daily=book[6]
            daily_names=["cash_before_open","equity_open_before_entries","market_value_open_before","positions_before",
                "cash_after_entries","market_value_after_entries","equity_after_entries","positions_during_session",
                "cash_close","market_value_close","equity_close","positions_close","entry_cash_out","exit_cash_in","realized_pnl","held_cost_basis"]
            write_rows(output/("cash-ledger-%s-s%d.csv"%(strategy,slots)),[dict(date=day,**{name:float(v) for name,v in zip(daily_names,values)}) for day,values in zip(days,daily)])
            picks={int(np.flatnonzero(daily[:,12]>0)[0]),int(np.argmax(book[1][1:])),int(np.argmax(1-book[1][1:]/np.maximum.accumulate(book[1])[1:])),len(days)-1}
            clipped=np.flatnonzero(book[5].astype(bool)&(book[7][:,4]<book[7][:,8]-np.maximum(1e-8,book[7][:,8]*1e-10)))
            if len(clipped):picks.add(int(book[7][clipped[0],0]))
            sample=[]
            for day in sorted(picks):
                for phase,eq,cash,gross,count in (("OPEN_AFTER_ENTRIES",daily[day,6],daily[day,4],daily[day,5],daily[day,7]),("CLOSE_AFTER_EXITS",daily[day,10],daily[day,8],daily[day,9],daily[day,11])):
                    sample.append({"strategy":strategy,"slots":slots,"date":days[day],"phase":phase,"equity":float(eq),"cash":float(cash),"open_positions":int(count),"invested_market_value":float(gross),"exposure":float(gross/eq) if eq else None})
            report["samples"].extend(sample)
            if strategy=="CONFLUENCE_SETUP_READY":
                top=sorted(positions,key=lambda p:p["net_pnl_usd"],reverse=True)
                for row in top[:20]:row["automated_quality_audit"]=case_quality(row)
                total=sum(r["net_pnl_usd"] for r in positions);positive=sum(max(0,r["net_pnl_usd"]) for r in positions)
                contribution={}
                for label,n in (("TOP_1",1),("TOP_10",10),("TOP_1_PERCENT",max(1,int(np.ceil(len(top)*.01))))):
                    amount=sum(r["net_pnl_usd"] for r in top[:n])
                    contribution[label]={"positions":n,"pnl_usd":amount,"share_of_total_net_pnl":amount/total if total else None,"share_of_gross_positive_pnl":amount/positive if positive else None}
                report["top_pnl"][str(slots)]={"top_20":top[:20],"attribution":contribution,"total_net_pnl":total}
                write_rows(output/("top-20-confluence-s%d.csv"%slots),[{k:(json.dumps(v,sort_keys=True) if isinstance(v,dict) else v) for k,v in row.items()} for row in top[:20]])
    con=load_pool(corrected,"CONFLUENCE_SETUP_READY")
    valid={k:v[con["quality"]==0] for k,v in con.items()}
    for kind in ("FULL","REMOVE_TOP_0.1_PERCENT","WINSOR_P99.9","WINSOR_P99"):
        derived,description=tail_variant(valid,kind)
        paths,offers,assets,idx=panel(derived,"VALID_ONLY",len(days),10)
        for slots in (5,10):
            book=historical(paths,offers,assets,slots,20,10000,.001,open_paths=derived["open_paths"][idx])
            m=history_metrics(book,paths,days,10000)
            report["sensitivity"].append({"kind":kind,"slots":slots,"diagnostic_only":True,"transform":description,"metrics":m})
    for name in ("SPY","QQQ"):
        b,curve,first=benchmark(base/"data/raw-daily"/(name+".json.gz"),meta,meta["test_first_index"])
        report["benchmarks"][name+"_FULL_PERIOD"]=b
    _,_,qqq_start=benchmark(base/"data/raw-daily/QQQ.json.gz",meta,meta["test_first_index"])
    common_days=meta["days"][qqq_start:meta["test_last_index"]+1]
    shift=qqq_start-meta["test_first_index"]
    for name in ("SPY","QQQ"):
        b,_,_=benchmark(base/"data/raw-daily"/(name+".json.gz"),meta,qqq_start)
        report["benchmarks"][name+"_QQQ_COMMON_PERIOD"]=b
    for strategy in NAMES:
        pool=load_pool(corrected,strategy)
        keep=pool["days"]>=shift
        subset={k:v[keep].copy() for k,v in pool.items()};subset["days"]-=shift
        paths,offers,assets,idx=panel(subset,"VALID_ONLY",len(common_days),10)
        for slots in (5,10):
            book=historical(paths,offers,assets,slots,20,10000,.001,open_paths=subset["open_paths"][idx])
            report["matched_qqq_portfolios"].append({"strategy":strategy,"slots":slots,"start":common_days[0],"end":common_days[-1],"metrics":history_metrics(book,paths,common_days,10000)})
    assert fingerprints()==before
    write_rows(output/"historical-account-days.csv",report["samples"])
    write(output/"audit.json",report)
    (output/"AUDIT.md").write_text(render(report))
    return report


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base",required=True);p.add_argument("--corrected",required=True);p.add_argument("--output",required=True)
    a=p.parse_args();run(a.base,a.corrected,a.output)
