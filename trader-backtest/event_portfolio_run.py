#!/usr/bin/env python3
"""Full daily EVENT_RETURN_PORTFOLIO analysis, separate from true-trade tests."""
import argparse
import csv
import datetime as dt
import hashlib
from pathlib import Path
import platform

import numpy as np

from common import digest, read, write
from event_portfolio_book import ACCELERATOR, bootstrap, historical
from event_portfolio_data import catalog, panel
from event_rehydrate import freeze
from event_signals import NAMES
from event_statistics import distribution
from event_quality_pit import annotate_pools, VINTAGE_STATUS
from portfolio_metrics import drawdowns
from portfolio_monte_carlo import quantiles
from run import fingerprints


def history_metrics(book,paths,days,initial):
    values,curve,positions,invested,pnl,accepted=book[:6]
    used=accepted.astype(bool)
    duration=(dt.date.fromisoformat(days[-1])-dt.date.fromisoformat(days[0])).days/365.2425
    dd=drawdowns(curve.tolist())
    returns=(paths[:,-1]-1)*100
    admitted=returns[used]
    pnl_used=pnl[used]
    positive=pnl_used[pnl_used>0]
    k=max(1,int(np.ceil(len(pnl_used)*.01))) if len(pnl_used) else 0
    top=np.sort(pnl_used)[-k:] if k else np.array([])
    return {"measurement":"EVENT_RETURN_PORTFOLIO","risk_per_trade":None,"stop_based_r_multiple":None,
        "end_capital":float(values[0]),"net_profit":float(values[0]-initial),
        "total_return_pct":float(100*(values[0]/initial-1)),
        "cagr":float((values[0]/initial)**(1/duration)-1) if duration>0 and values[0]>0 else None,
        "max_drawdown":float(values[1]),"average_drawdown":dd["average"],
        "longest_drawdown_sessions":dd["longest_observations"],"exposure":float(values[2]),
        "average_positions":float(values[3]),"maximum_positions":int(values[4]),
        "closed_event_positions":int(values[5]),"win_rate_net_of_modelled_costs":float(values[6]/values[5]) if values[5] else None,
        "modelled_costs_paid_usd":float(values[7]),"admitted_events":int(values[8]),
        "cash_rejections":int(values[9]),"duplicate_rejections":int(values[10]),
        "remaining_cash":float(values[11]),"minimum_cash":float(values[12]),"maximum_observed_exposure":float(values[13]),
        "duplicate_active_violations":int(values[14]),"maximum_entry_cash_fraction":float(values[15]),
        "open_close_phase_max_drawdown":float(values[16]),
        "exposure_note":"Mean post-entry open exposure during held sessions; primary drawdown remains daily close",
        "cagr_over_max_drawdown":float(((values[0]/initial)**(1/duration)-1)/values[1]) if duration>0 and values[0]>0 and values[1]>0 else None,
        "event_pool_mean_return_pct":float(np.mean(returns)) if len(returns) else None,
        "event_pool_median_return_pct":float(np.median(returns)) if len(returns) else None,
        "admitted_mean_gross_event_return_pct":float(np.mean(admitted)) if len(admitted) else None,
        "admitted_median_gross_event_return_pct":float(np.median(admitted)) if len(admitted) else None,
        "event_pool_distribution":distribution(returns),
        "portfolio_tail_attribution":{"top_one_percent_admitted_positions":k,
            "top_one_percent_pnl_usd":float(top.sum()),
            "top_one_percent_share_of_gross_positive_pnl":float(top[top>0].sum()/positive.sum()) if positive.sum()>0 else None,
            "top_one_percent_share_of_total_net_pnl":float(top.sum()/pnl_used.sum()) if pnl_used.sum()!=0 else None,
            "note":"Attribution to the unmodified portfolio, not removal/reoptimization. Dollar P&L also reflects wealth compounding and allocation size."}}


def monte_metrics(values,initial,method,config):
    capital,dd=values[:,0],values[:,1]
    return {"measurement":"EVENT_RETURN_PORTFOLIO","method":method,"paths":len(values),
        "seed":config["seed"],"block_sessions":config["block_sessions"] if method=="SESSION_BLOCK" else None,
        "risk_per_trade":None,"median_end_capital":float(np.median(capital)),"mean_end_capital":float(np.mean(capital)),
        "end_capital_quantiles":quantiles(capital),"return_pct_quantiles":quantiles(100*(capital/initial-1)),
        "max_drawdown_quantiles":quantiles(dd),"mean_max_drawdown":float(np.mean(dd)),
        "probabilities":{"negative_total_performance":float(np.mean(capital<initial)),
            **{"drawdown_gt_%d_pct"%p:float(np.mean(dd>p/100)) for p in (5,10,20,30)},
            **{"gain_ge_%d_pct"%p:float(np.mean(capital>=initial*(1+p/100))) for p in (25,50,100)}},
        "closed_position_quantiles":quantiles(values[:,5]),"average_exposure":float(np.mean(values[:,2])),
        "maximum_observed_positions":int(np.max(values[:,4])),"flags":["EVENT_RETURN_SIMULATION_NOT_TRUE_TRADES",
            "RETROSPECTIVE_VALID_SELECTION","EMPIRICAL_SAMPLE_FREQUENCIES_NOT_GUARANTEES"]}


def true_trade_inventory(prior_report=None):
    counts={}
    if prior_report and Path(prior_report).exists():
        old=read(prior_report)
        for row in old["results"]:
            if row["variant"]=="COOLDOWN20":
                counts[row["strategy"]]=max(counts.get(row["strategy"],0),row["historical"].get("trades",0))
    return {strategy:{"measurement":"TRUE_TRADE_PORTFOLIO","status":"INSUFFICIENT_TRADE_DATA",
        "reason":"Rehydrated full universe has daily close events, not historical entry/stop/intraday trigger plans",
        "full_universe_true_trades":0,"prior_small_universe_technical_trades":counts.get(strategy,0),
        "prior_sample_status":"VERY_LOW_SAMPLE","prior_interpretation":"TECHNICAL_FUNCTION_TEST_ONLY",
        "monte_carlo":{"status":"NOT_RUN_INSUFFICIENT_TRADE_DATA","paths":0},
        "thresholds":{"under_30":"VERY_LOW_SAMPLE_NO_STRATEGY_MC","30_to_99":"LOW_SAMPLE","100_plus":"REGULAR_EXPLORATORY"}}
        for strategy in NAMES}


def paired_comparisons(results,simulations,config):
    out={}
    for slots in config["slots"]:
        selected={r["strategy"]:r for r in results if r["horizon"]==config["primary_horizon"] and
                  r["quality"]==config["primary_quality"] and r["slots"]==slots and r["costs"]=="REALISTIC"}
        if len(selected)!=3:
            continue
        con=selected["CONFLUENCE_SETUP_READY"]
        for other in ("MINERVINI","BREAKOUT_SETUP_READY"):
            peer=selected[other]
            key="%d/CONFLUENCE_vs_%s"%(slots,other)
            row={"historical_end_capital_ratio":con["metrics"]["end_capital"]/peer["metrics"]["end_capital"],
                "historical_cagr_difference_pp":100*(con["metrics"]["cagr"]-peer["metrics"]["cagr"]),
                "historical_max_drawdown_difference_pp":100*(con["metrics"]["max_drawdown"]-peer["metrics"]["max_drawdown"]),
                "bootstrap":{}}
            for method in ("IID_EVENT_PATH","SESSION_BLOCK"):
                a=simulations.get((slots,"CONFLUENCE_SETUP_READY",method))
                b=simulations.get((slots,other,method))
                if a is not None and b is not None:
                    row["bootstrap"][method]={"fraction_confluence_higher_terminal_capital":float(np.mean(a[:,0]>b[:,0])),
                        "median_terminal_capital_ratio":float(np.median(a[:,0]/b[:,0])),
                        "fraction_confluence_lower_max_drawdown":float(np.mean(a[:,1]<b[:,1])),
                        "pairing_note":"Session blocks use identical sampled calendar blocks across strategies; IID outcomes have no cross-strategy market pairing interpretation"}
            out[key]=row
    return out


def render(report):
    c=report["configuration"]
    percent=lambda x:"n/v" if x is None else "%.2f%%"%(100*x)
    lines=["# Erweiterte Event-Return-Portfolioanalyse","",
        "## Datenbasis und feste Regeln","",
        "Startkapital %.2f USD; Zeitraum %s bis %s; %d angefragte und %d auswertbare Ticker."%(
            c["initial_capital"],*report["period"],report["rehydration"]["requested"],report["rehydration"]["eligible"]),
        "Die ursprünglichen Roharchive fehlten. Neuer Nasdaq-/Yahoo-Snapshot mit unveränderten nativen Regeln; Mitgliederidentität zur alten Liste ist ohne deren Manifest nicht beweisbar.","",
        "Primäre Qualitätsmethodik: %s, COOLDOWN20, 20 Sessions. PIT_SAFE verwendet nur OHLCV-Auffälligkeiten bis Signal-Close T. Datenstatus: %s. Ohne archivierte damalige Kursstände keine zertifizierte historische PIT-Strategieperformance. Weitere Horizonte und 5/10 Slots sind vorab festgelegt. Equal Weight bei Eintritt, Drift erlaubt, Cash-Limit, keine doppelte aktive Tickerposition, kein Hebel. Rangfolge: Status, RS_PROXY, Liquidität, Symbol."%(c["primary_quality"],VINTAGE_STATUS),"",
        "VALID prüft rückblickend das 260-Session-Featurefenster und den gesamten späteren Return-Verlauf. Dies ist keine live umsetzbare Point-in-Time-Auswahl. ALL bleibt separate Sensitivität; kein Ticker wird manuell gelöscht.","",
        "REALISTIC ist ein Daily-Kostenszenario: 10 bps voller Spread plus 5 bps Slippage je Seite. Historische tatsächliche Spreads/Slippage und Provisionssätze sind unbekannt. Die share-basierte Pilotgebühr wird ohne verlässliche historische Stückzahl-/Kontraktbasis nicht als beobachtete Kosten behauptet. Ergebnisse nach modellierter Friktion, vor unbekannten tatsächlichen Provisionen und Steuern.","",
        "Flags: SURVIVORSHIP_BIAS_RISK, REVISED_DATA_NOT_POINT_IN_TIME, NO_HISTORICAL_SECURITY_IDENTIFIER_ARCHIVE, RETROSPECTIVE_VALID_SELECTION, EVENT_RETURN_SIMULATION_NOT_TRUE_TRADES.","",
        "### Rehydrierte Event-Zahlen","","| Strategie | frühere Richtgröße | neue Cooldown-Events | VALID mit 20-Session-Outcome |","|---|---:|---:|---:|"]
    for name in NAMES:
        row=next(r for r in report["event_counts"] if r["strategy"]==name and r["horizon"]==20)
        lines.append("| %s | %d | %d | %d |"%(name,c["expected_previous_counts"][name],row["native_cooldown_events"],row["quality_counts"]["VALID"]))
    lines += ["","Abweichungen können aus revidierten Vendor-Preisen, Listingwechseln, Abruffehlern, unterschiedlichen verfügbaren Historien und Quality-/Horizon-Censoring stammen. Details stehen in acquisition.json, ticker-coverage.json und event-statistics.json. Zielzahlen werden nicht durch Regeländerungen erzwungen.","",
        "## Event-Return Portfolio","",
        "**EVENT_RETURN_PORTFOLIO**: Signal am Close T, Entry zum beobachteten Open T+1, Session 1 ist der Entry-Tag, Exit zum Close T+h. Keine Stops oder stopbasierten Risikoprozentwerte. Cash aus einem Exit steht erst beim folgenden Open fuer neue Entries bereit. Kursbasierte Execution-Proxys, keine garantierten tatsaechlichen Fills.","",
        "| Strategie | Slots | Kapital USD | CAGR | MaxDD | Win Rate | Exposure | zugelassene Events | Mittel/Median primärer Event-Returns | Kostenwirkung ggü. IDEAL |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    primary=[r for r in report["portfolios"] if r["horizon"]==20 and r["quality"]==c["primary_quality"] and r["costs"]=="REALISTIC"]
    for row in primary:
        m=row["metrics"]
        lines.append("| %s | %d | %.2f | %s | %s | %s | %s | %d | %.2f%% / %.2f%% | %s |"%(
            row["strategy"],row["slots"],m["end_capital"],percent(m["cagr"]),percent(m["max_drawdown"]),
            percent(m["win_rate_net_of_modelled_costs"]),percent(m["exposure"]),m["admitted_events"],
            m["event_pool_mean_return_pct"],m["event_pool_median_return_pct"],percent(row["cost_effect_terminal_fraction"])))
    lines += ["","## Event Monte Carlo","",
        "IID zieht vollständige beobachtete Event-Kursverläufe aus dem primären Qualitätspool und hält die historischen Signaltermine, Ränge und Tickeridentitäten fest. SESSION_BLOCK zieht 60 zusammenhängende Börsensitzungen mit allen Kandidaten und Leertagen. Beide wenden Equal Weight, Cash-, Slot- und Tickergrenzen auf jedem Pfad erneut an. Keine Normalverteilung, kein stopbasiertes Trade-Modell. Abhängigkeiten an Blockgrenzen werden nicht erhalten.","",
        "| Strategie | Slots | Methode | Pfade | Median Kapital | P01 | P05 | P10 | P25 | P50 | P75 | P90 | P95 | P99 |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in report["monte_carlo"]:
        m=r["metrics"]
        lines.append("| %s | %d | %s | %d | %.2f | %s |"%(r["strategy"],r["slots"],r["method"],m["paths"],m["median_end_capital"],
            " | ".join("%.2f"%m["end_capital_quantiles"]["P%02d"%q] for q in (1,5,10,25,50,75,90,95,99))))
    lines += ["","| Strategie / Slots / Methode | P(Verlust) | DD P50 | DD P95 | DD >5% | >10% | >20% | >30% | Gewinn >=25% | >=50% | >=100% |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in report["monte_carlo"]:
        m=r["metrics"];p=m["probabilities"]
        vals=[p["negative_total_performance"],m["max_drawdown_quantiles"]["P50"],m["max_drawdown_quantiles"]["P95"]]+[p["drawdown_gt_%d_pct"%q] for q in (5,10,20,30)]+[p["gain_ge_%d_pct"%q] for q in (25,50,100)]
        lines.append("| %s / %d / %s | %s |"%(r["strategy"],r["slots"],r["method"]," | ".join(percent(v) for v in vals)))
    lines += ["","## True Trade Portfolio","",
        "**INSUFFICIENT_TRADE_DATA** für den vollen Vergleich. Daily SETUP_READY/PASS liefert weder den damaligen Intraday-Entry noch Stop und Exit-Plan. Die früheren fünf Minervini-Trades stammen aus einem anderen kleinen Ranking-Universum und sind ausschließlich TECHNICAL_FUNCTION_TEST / VERY_LOW_SAMPLE.","",
        "## True Trade Monte Carlo","",
        "Nicht als Strategieanalyse ausgeführt. Unter 30 echten Trades: VERY_LOW_SAMPLE und kein Strategie-Monte-Carlo; 30–99: LOW_SAMPLE; ab 100: reguläre explorative Simulation. Für den rehydrierten vollen Daily-Datensatz liegen keine hinreichenden echten Trade-Pläne vor.","",
        "## Confluence-Vergleich und Extremgewinner","",
        "| Vergleich | historischer Kapitalfaktor Confluence/Peer | CAGR-Differenz pp | MaxDD-Differenz pp | P(Confluence mehr Kapital), Block-Bootstrap | P(Confluence kleinerer DD), Block-Bootstrap |",
        "|---|---:|---:|---:|---:|---:|"]
    for key,row in report["comparisons"].items():
        b=row["bootstrap"].get("SESSION_BLOCK",{})
        lines.append("| %s | %.4f | %.2f | %.2f | %s | %s |"%(key,row["historical_end_capital_ratio"],row["historical_cagr_difference_pp"],
            row["historical_max_drawdown_difference_pp"],percent(b.get("fraction_confluence_higher_terminal_capital")),percent(b.get("fraction_confluence_lower_max_drawdown"))))
    lines += ["","Negative MaxDD-Differenzen bedeuten geringeren Drawdown bei Confluence. Die Paare vergleichen identische Kalenderblöcke und Portfolioparameter; sie sind keine kausale Filtereffekt-Schätzung.","",
        "| Strategie / Slots | Top 1%: Anteil am positiven Portfolio-P&L | Top 1%: Anteil am Netto-P&L | Top 1% Event-Returns: Beitrag zum unmodifizierten Mittel (pp) | Median Event-Return |",
        "|---|---:|---:|---:|---:|"]
    for row in primary:
        m=row["metrics"];tail=m["portfolio_tail_attribution"];dist=m["event_pool_distribution"]
        lines.append("| %s / %d | %s | %s | %.4f | %.4f%% |"%(row["strategy"],row["slots"],percent(tail["top_one_percent_share_of_gross_positive_pnl"]),
            percent(tail["top_one_percent_share_of_total_net_pnl"]),dist["tail"]["top_one_percent_mean_contribution_pp"],dist["median"]))
    lines += ["","Die Tail-Zerlegung verändert keinen Trade, Event, Preis oder Rang. Dollar-P&L hängt auch vom später höheren Kapital ab; die Event-Return-Zerlegung zeigt zusätzlich die ungewichtete Konzentration.","",
        "Belastbar sind Reproduzierbarkeit, die beobachtete Portfolio-Arithmetik und die beschriebenen Unterschiede innerhalb dieses eingefrorenen Datensatzes. Strategieprofitabilität, zukünftige Ruinwahrscheinlichkeit und ein kausaler Confluence-Vorteil bleiben wegen Survivorship Bias, revidierter Daten, fehlender historischer Identitäten, rückblickender VALID-Auswahl und modellierter Kosten explorativ. 10.000 Resamples beseitigen diese Grenzen nicht.","",
        "Keine Optimierung, Orders, Broker-Aufrufe, Commits oder Pushes.",""]
    return "\n".join(lines)


def run(base,config,output):
    base,output=Path(base),Path(output)
    output.mkdir(parents=True,exist_ok=True)
    if config["measurement"]!="EVENT_RETURN_PORTFOLIO" or config["account_currency"]!="USD" or config["leverage"]!=1:
        raise ValueError("this frozen analysis requires an unlevered USD event-return book")
    if config.get("execution")!="SIGNAL_CLOSE_T_ENTRY_OPEN_T_PLUS_1_EXIT_CLOSE_T_PLUS_H":
        raise ValueError("explicit next-open execution protocol required")
    if config.get("primary_quality")!="PIT_SAFE":
        raise ValueError("PIT_SAFE must be the primary quality method; legacy VALID is sensitivity only")
    if config["ranking"]!=["trigger_setup_status","RS_PROXY","liquidity","symbol"]:
        raise ValueError("predeclared ranking changed")
    if config["paths"]<10000:
        raise ValueError("full event analysis requires at least 10000 paths per method")
    analysis,events=base/"analysis",base/"events"
    meta=read(analysis/"matrix-metadata.json")
    sources=fingerprints()
    matrix_inputs={}
    names=[analysis/"close.f64",analysis/"ranks.f64",analysis/"forward-contiguous.u8",events/"events-COOLDOWN20.u8"]
    names += [events/("quality-window-%d.u8"%h) for h in config["horizons"]]
    names += sorted((analysis/"stock-cache").glob("*.npz"))
    names += [base/"data/acquisition.json"]
    for path in names:
        sha=hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b""):
                sha.update(chunk)
        matrix_inputs[str(path.relative_to(base))]={"bytes":path.stat().st_size,"sha256":sha.hexdigest()}
    write(output/"matrix-input-sha256.json",matrix_inputs)
    code={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(__file__).parent.glob("event_portfolio*.py"))}
    code["event_rehydrate.py"]=hashlib.sha256((Path(__file__).parent/"event_rehydrate.py").read_bytes()).hexdigest()
    code["event_quality_pit.py"]=hashlib.sha256((Path(__file__).parent/"event_quality_pit.py").read_bytes()).hexdigest()
    protocol={"configuration":config,"analysis_sha256":digest(meta),"event_sha256":digest(read(events/"event-metadata.json")),
        "quality_methodology_sha256":digest(read(events/"frozen-methodology.json")),"native_sources":sources,"implementation":code,
        "matrix_inputs":matrix_inputs,
        "quality":"Primary PIT_SAFE: raw OHLCV diagnostics only over trailing 260 sessions through signal close T; no future bars/actions/metadata. Snapshot vintages are NOT PIT certified. ALL/legacy VALID/INVALID_ONLY_PIT sensitivities separate",
        "anchor":"Signal close T; observed open T+1 entry; entry counts as session one; scheduled close T+h exit. No same-close fills or early reuse of exit proceeds",
        "costs":"Existing 10bps full spread and 5bps per-side slippage as a daily bps proxy; unknown historical commissions not fabricated",
        "iid":"Full quality-qualified event outcome paths, fixed observed signal calendar/ranking/identities, book limits reapplied",
        "blocks":"Whole 60-session moving calendar blocks, clustered offers and observed full outcome paths, book limits reapplied"}
    freeze(output/"frozen-protocol.json",protocol)
    first,last=meta["test_first_index"],meta["test_last_index"]
    days=meta["days"][first:last+1]
    report={"configuration":config,"period":[days[0],days[-1]],"rehydration":read(base/"rehydration-audit.json"),
        "event_counts":[],"portfolios":[],"monte_carlo":[],"true_trade":true_trade_inventory(base.parent/"portfolio-first"/"results"/"report.json"),
        "accelerator":ACCELERATOR,"native_sources_unchanged":True}
    simulations={}
    for horizon in config["horizons"]:
        for strategy in NAMES:
            pool,counts=catalog(analysis,events,output/"catalog",strategy,horizon)
            pool=annotate_pools({strategy:pool},base,horizon)[strategy]
            report["event_counts"].append(dict(counts,strategy=strategy,horizon=horizon))
            for quality in config["quality_panels"]:
                if quality=="ALL" and horizon!=config["primary_horizon"]:
                    continue
                paths,offers,assets,indices=panel(pool,quality,len(days),max(config["slots"]))
                if not len(paths):
                    raise ValueError("empty event pool: "+strategy+" "+str(horizon)+" "+quality)
                for slots in config["slots"]:
                    ideal=None
                    for costs in config["cost_scenarios"]:
                        side_cost=0 if costs=="IDEAL" else (config["spread_bps"]/2+config["slippage_bps_per_side"])/10000
                        key="%s-h%d-%s-s%d-%s"%(strategy,horizon,quality,slots,costs)
                        open_paths=np.ascontiguousarray(pool["open_paths"][indices])
                        book=historical(paths,offers,assets,slots,horizon,config["initial_capital"],side_cost,open_paths=open_paths)
                        m=history_metrics(book,paths,days,config["initial_capital"])
                        if costs=="IDEAL":ideal=m["end_capital"]
                        row={"strategy":strategy,"horizon":horizon,"quality":quality,"slots":slots,"costs":costs,"metrics":m,
                            "cost_effect_terminal_fraction":m["end_capital"]/ideal-1 if ideal else None}
                        report["portfolios"].append(row)
                        np.savez_compressed(output/("book-"+key+".npz"),values=book[0],equity=book[1],positions=book[2],invested=book[3],pnl=book[4],accepted=book[5],pool_indices=indices,daily=book[6],ledger=book[7])
                        with (output/("equity-"+key+".csv")).open("w",newline="") as f:
                            writer=csv.writer(f);writer.writerow(["session","equity_usd","positions"])
                            for day,equity,positions in zip(days,book[1][1:],book[2]):writer.writerow([day,equity,int(positions)])
                        if horizon==config["primary_horizon"] and quality==config["primary_quality"] and costs=="REALISTIC":
                            for method in ("IID_EVENT_PATH","SESSION_BLOCK"):
                                checkpoint=output/("bootstrap-%s-s%d-%s.npz"%(strategy,slots,method))
                                if checkpoint.exists():
                                    values=np.load(checkpoint)["values"]
                                else:
                                    print("Event portfolio bootstrap",strategy,slots,method,config["paths"],flush=True)
                                    values=bootstrap(paths,offers,assets,slots,horizon,config["initial_capital"],side_cost,method,
                                        config["seed"],config["block_sessions"],config["paths"],open_paths=open_paths)
                                    np.savez_compressed(checkpoint,values=values)
                                if len(values)!=config["paths"]:
                                    raise ValueError("bootstrap checkpoint path count mismatch")
                                simulations[slots,strategy,method]=values
                                report["monte_carlo"].append({"strategy":strategy,"slots":slots,"method":method,
                                    "metrics":monte_metrics(values,config["initial_capital"],method,config)})
                        write(output/"report.json",report)
                        print("Event book",key,"events",m["admitted_events"],"capital",round(m["end_capital"],2),flush=True)
    report["comparisons"]=paired_comparisons(report["portfolios"],simulations,config)
    write(output/"report.json",report)
    (output/"REPORT.md").write_text(render(report))
    if fingerprints()!=sources:
        raise RuntimeError("native strategy sources changed during event study")
    write(output/"verification.json",{"protocol_sha256":digest(protocol),"report_sha256":digest(report),
        "native_sources_unchanged":True,"implementation":code,"python":platform.python_version(),"numpy":np.__version__,
        "completed_event_bootstrap_paths":sum(m["metrics"]["paths"] for m in report["monte_carlo"]),
        "true_trade_strategy_bootstrap_paths":0})
    return report


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base",required=True)
    p.add_argument("--output",required=True)
    p.add_argument("--config",default=str(Path(__file__).parent/"event-portfolio-config.json"))
    a=p.parse_args()
    run(a.base,read(a.config),a.output)
