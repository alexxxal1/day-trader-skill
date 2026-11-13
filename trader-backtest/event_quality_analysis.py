#!/usr/bin/env python3
"""Frozen quality-filter comparison, rule attribution and extreme-case audit.

No new signals, strategy parameters, price repairs, broker or network calls.
"""
import argparse
import csv
import hashlib
import math
from pathlib import Path

import numpy as np

from common import digest,read,write
from data_quality import REASONS
from event_portfolio_audit import validate_book
from event_portfolio_book import historical
from event_portfolio_data import panel
from event_portfolio_run import history_metrics
from event_quality_pit import (CAUSAL_BITS,CAUSAL_INVALID_BITS,VARIANTS,VINTAGE_STATUS,
                               annotate_pools,quality_mask,rule_inventory)
from event_rehydrate import freeze
from event_signals import NAMES
from run import fingerprints


def selected_book(pool,mask,days,slots):
    # Filtering preserves native sort order. panel queues signal T at open T+1.
    indices=np.flatnonzero(mask)
    selected={k:v[indices] for k,v in pool.items()}
    paths,offers,assets,_=panel(selected,"ALL",len(days),10)
    if not len(paths):raise ValueError("empty quality panel")
    book=historical(paths,offers,assets,slots,20,10000,.001,
                    open_paths=np.ascontiguousarray(selected["open_paths"]))
    return book,history_metrics(book,paths,days,10000),indices


def save_book(path,book,indices):
    np.savez_compressed(path,values=book[0],equity=book[1],positions=book[2],invested=book[3],
                        pnl=book[4],accepted=book[5],pool_indices=indices,daily=book[6],ledger=book[7])


def flag_detail(pool,index):
    past=int(pool["legacy_feature_reasons"][index]);future=int(pool["legacy_outcome_reasons"][index])
    pit=int(pool["pit_reasons"][index]);inventory={r['bit']:r for r in rule_inventory() if r['bit'] is not None}
    result=[]
    for bit,name in REASONS.items():
        if not (past|future|pit)&bit:continue
        rule=inventory[bit];observed=bool(pit&bit)
        result.append({'flag':name,'legacy_feature':bool(past&bit),'legacy_future_outcome':bool(future&bit),
            'causal_feature':observed,'reason':rule['rule'],
            'knowable_by_entry':'YES_IF_CONTEMPORANEOUS_OHLCV' if observed else 'NO_FUTURE_BAR' if future&bit and not past&bit else rule['classification'],
            'historical_vintage_certified':False})
    return result


def render(report):
    pct=lambda n:'%.2f%%'%(100*n)
    lines=['# Methodischer Audit des VALID-Filters','',
        '**Primäre Qualitätsmethodik: PIT_SAFE.** Ergebnisstatus: '+VINTAGE_STATUS+'. Der Filter ist entscheidungszeitlich kausal implementiert; historische Veröffentlichungs-/Revisionsstände fehlen. Die Zahlen sind bedingte Snapshot-Rekonstruktionen, keine zertifizierte damalige Strategieperformance. Das bisherige VALID-Ergebnis ist ausschließlich retrospektive Sensitivität.','',
        '10.000 USD; 1993-01-29 bis 2026-11-06; unveränderte COOLDOWN20-Events und Rankings; Open T+1 bis Close T+20 (Entry-Tag zählt als Session 1); 5/10 Slots; Cash-Cap; REALISTIC unverändert 10 bps voller Spread und 5 bps Slippage je Seite. Keine Stops oder Risikoprozentwerte.','',
        '## Vollständiges Regelinventar','',
        'Die Klassifikation POINT_IN_TIME_SAFE bezieht sich auf die Rechenregel bei tatsächlich zeitgerecht beobachteten Bars. Sie zertifiziert nicht die Verfügbarkeit der heute revidierten Eingabedaten. Sobald der alte Filter eine solche Regel auf spätere Outcome-Bars anwendet, ist die Event-Auswahl RETROSPECTIVE. VALID=keine Regel im kombinierten Fenster; SUSPECT=höchste Schwere 1; INVALID=höchste Schwere 2. Höhere Schwere gewinnt.','',
        '| Flag | Schwere | Regel und Daten | Klassifikation | Verfügbarkeit / Abhängigkeit |','|---|---|---|---|---|']
    for r in report['rules']:
        lines.append('| %s | %s | %s; %s | %s | %s |'%(r['flag'],r['severity'],r['rule'],r['data'],r['classification'],r['availability']))
    lines+=['','Reverse Splits allein sind kein Ausschluss. Ticker-Reuse und fehlendes Merger-/Security-Master-Archiv sind globale Grenzen; es gibt keine vollständige entsprechende Ausschlussregel. Nachträgliche SEC-Abgleiche werden von diesem Filter nicht verwendet. Extreme Forward Returns allein ändern keinen Qualitätsstatus. Keine manuellen Ticker-Ausschlüsse.','',
        '## Vier Varianten','',
        '- ALL: alle im unveränderten Basiskatalog berechenbaren Events.','- PIT_SAFE: keine ausschließlich aus OHLCV erkennbare numerische, Jump-, Zero-Volume- oder Stale-Auffälligkeit in den 260 Sessions bis einschließlich Signal T. Die Schwellen entsprechen exakt den bestehenden QC-Regeln.','- VALID_ONLY: bisheriges Maximum der QC-Schwere über Featurefenster und zukünftige 20 Sessions, inklusive aktueller Metadaten/Action-Diagnosen.','- INVALID_ONLY_PIT: entfernt ausschließlich bis T erkennbare numerisch ungültige/fehlende Bars im Featurefenster; SUSPECT bleibt enthalten.','',
        'Verwendete/exkludierte Pool-Events beziehen sich auf ALL nach getrennt ausgewiesenem technischem Outcome-Censoring; zugelassen sind tatsächlich im Portfolio gehaltene Event-Positionen. Win Rate ist nach modellierten Kosten; Median ist der Brutto-Return des gesamten jeweiligen Event-Pools (kein Trade-R).','',
        '| Strategie | Slots | Variante | CAGR | MaxDD | Endkapital USD | Win Rate | Median Event-Return | Pool verwendet | QC-exkludiert | Portfolio-Positionen |',
        '|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in report['portfolios']:
        m=r['metrics'];lines.append('| %s | %d | %s | %s | %s | %.2f | %s | %.4f%% | %d | %d | %d |'%(
            r['strategy'],r['slots'],r['variant'],pct(m['cagr']),pct(m['max_drawdown']),m['end_capital'],
            pct(m['win_rate_net_of_modelled_costs']),m['event_pool_median_return_pct'],r['pool_used'],r['excluded_events'],m['admitted_events']))
    lines+=['','## Welche Regel erklärt den Abstand?','',
        'Die Einzelregel-Tabellen geben erstens die Anzahl betroffener Events und deren tatsächlich realisierten P&L-Beitrag im unveränderten ALL-Buch an. Diese Beiträge überlappen und sind keine additive Gegenrechnung. Zweitens wird jedes Buch mit genau dieser einen ausgeschlossenen Regel neu simuliert. Drittens wird in der **vorab fixierten Bit-Reihenfolge** eine kumulative Ausschlusszerlegung berechnet. Deren Endkapital-Deltas summieren sich exakt von ALL zu VALID, sind aber reihenfolgeabhängig: geänderte Admissions und Zinseszins verhindern eine eindeutige kausale Dollarzuordnung. Keine Regel wird nach Performance ausgewählt. Vollständig für alle Strategien/Slots: rule-effects.csv und report.json.','',
        '| Confluence Slots | Regel | Events betroffen | nur Zukunftsfenster | betroffene ALL-Positionen | deren ALL-P&L USD | CAGR nur diese Regel entfernt | MaxDD | kumulatives Endkapital-Delta USD |',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for r in report['rule_effects']:
        if r['strategy']!='CONFLUENCE_SETUP_READY':continue
        lines.append('| %d | %s | %d | %d | %d | %.2f | %s | %s | %.2f |'%(r['slots'],r['rule'],r['excluded_by_rule'],r['future_only_events'],
            r['affected_all_positions'],r['all_realized_pnl_of_affected_positions'],pct(r['single_rule_metrics']['cagr']),pct(r['single_rule_metrics']['max_drawdown']),r['sequential_end_capital_delta']))
    lines+=['','### Vergangenheit / spätere Outcome-Bars','',
        'PAST_ONLY_LEGACY_DIAGNOSTIC prüft nur das Featurefenster der alten Flags. Es bleibt wegen Metadaten/Action-Abhängigkeiten retrospektiv. Sein Vergleich mit VALID isoliert rechnerisch die zusätzliche Selektion durch zukünftige Outcome-Bars bei unverändertem Featurefilter.','',
        '| Strategie | Slots | Feature-only: Pool / CAGR / Endkapital | zusätzlich durch Outcome ausgeschlossene Events | VALID CAGR / Endkapital |','|---|---:|---|---:|---|']
    for r in report['past_only_legacy']:
        v=next(p for p in report['portfolios'] if p['strategy']==r['strategy'] and p['slots']==r['slots'] and p['variant']=='VALID_ONLY')
        lines.append('| %s | %d | %d / %s / %.2f | %d | %s / %.2f |'%(r['strategy'],r['slots'],r['pool_used'],pct(r['metrics']['cagr']),r['metrics']['end_capital'],r['pool_used']-v['pool_used'],pct(v['metrics']['cagr']),v['metrics']['end_capital']))
    lines+=['','## Extreme Confluence-Beiträge','',
        'Jeweils die größten 20 positiven und 20 negativen P&L-Beiträge in ALL, PIT_SAFE und VALID, für beide Slotzahlen, stehen vollständig in extreme-confluence.json und .csv. Status ist die Filterzulassung desselben Events, nicht seine tatsächliche Zulassung in einem anderen Slotwettbewerb. Keine Fälle wurden gelöscht. Folgend die zehn größten Gewinner/Verlierer im ALL-5-Slot-Buch.','',
        '| Seite | Ticker | Signal | Entry | Exit | P&L USD | Return | ALL / PIT_SAFE / VALID zulässig | Flags und Kenntnis zum Entry |','|---|---|---|---|---|---:|---:|---|---|']
    for r in report['extremes']:
        if r['slots']!=5 or r['source_book']!='ALL' or r['rank']>10:continue
        desc='; '.join(f['flag']+': '+f['knowable_by_entry'] for f in r['flags']) or 'keine lokalen Flags; historische Identität unbestätigt'
        lines.append('| %s | %s | %s | %s | %s | %.2f | %.2f%% | %s / %s / %s | %s |'%(r['side'],r['ticker'],r['signal_day'],r['entry_day'],r['exit_day'],r['net_pnl_usd'],r['gross_event_return_pct'],r['ALL'],r['PIT_SAFE'],r['VALID_ONLY'],desc))
    lines+=['','## Grenzen und Primärdarstellung','',
        'PIT_SAFE verwendet weder spätere Actions/SEC-Abgleiche noch Vendor-Metadaten oder spätere Preisbars zur Qualitätsentscheidung. Ein optionaler Archivadapter akzeptiert ausschließlich Revisionen mit nachgewiesenem Veröffentlichungszeitpunkt <= Entscheidungszeitpunkt; der heutige Snapshot wird nicht mit erfundenen Verfügbarkeitszeiten versehen. Änderungen bereits vergangener Kurse durch späteren Vendor-Download bleiben ohne damalige Vintages grundsätzlich nicht nachweisbar oder rückgängig zu machen.','',
        'Das native Signaluniversum, die heutigen split-adjustierten OHLCV, heutige Listing-Mitgliedschaft und ursprüngliche Eligible-Regeln bleiben unverändert. Diese können selbst Nicht-PIT-Einflüsse enthalten. ALL setzt zudem technisch vollständige positive Open-/Close-Verläufe voraus; fehlende oder rechtszensierte Outcomes werden separat gezählt, nicht als damals bekannter Ausschluss ausgegeben. Auch PIT_SAFE ist daher keine vollständige historische PIT-Zertifizierung.','',
        'Für künftige Reports ist PIT_SAFE die primäre **Qualitätsmethodik**, mit ausdrücklichem Snapshot-Vorbehalt. ALL, VALID und INVALID_ONLY_PIT sind Sensitivitäten. Retrospektives VALID darf keine primäre historische Strategieperformance mehr darstellen. Kein neuer Monte-Carlo-Test in diesem ausschließlich auf Qualitätsselektion begrenzten Audit; alte VALID-Bootstraps sind keine PIT_SAFE-Ergebnisse.','',
        'SURVIVORSHIP_BIAS_RISK; REVISED_DATA_NOT_POINT_IN_TIME; NO_HISTORICAL_SECURITY_IDENTIFIER_ARCHIVE; OUTCOME_COMPLETENESS_SELECTION; EVENT_RETURN_SIMULATION_NOT_TRUE_TRADES. Keine Strategieoptimierung, kein Commit, Push, Order oder Broker.','']
    return '\n'.join(lines)


def run(base,baseline,output):
    base,baseline,output=Path(base),Path(baseline),Path(output);output.mkdir(parents=True,exist_ok=True)
    meta=read(base/'analysis/matrix-metadata.json');first,last=meta['test_first_index'],meta['test_last_index'];days=meta['days'][first:last+1]
    code={name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest() for name in (
        'event_quality_pit.py','event_quality_analysis.py','event_portfolio_book.py','event_portfolio_data.py','event_portfolio_run.py')}
    sources=fingerprints()
    pools={}
    pool_hashes={}
    for name in NAMES:
        p=baseline/'catalog'/('pool-%s-20.npz'%name)
        with np.load(p) as z:pools[name]={k:z[k] for k in z.files}
        pool_hashes[p.name]=hashlib.sha256(p.read_bytes()).hexdigest()
    plan={'purpose':'VALID selection/hindsight audit only; no strategy tuning','primary_quality':'PIT_SAFE',
          'data_vintage_status':VINTAGE_STATUS,'period':[days[0],days[-1]],'horizon':20,'initial':10000,'side_cost':.001,
          'slots':[5,10],'variants':VARIANTS,'rules':rule_inventory(),'feature_window':260,
          'sequential_order':list(REASONS.values()),'analyses':['all four panels','each legacy rule alone','cumulative rules in ascending bit order','legacy past-only','top/bottom 20 Confluence P&L per panel and slots'],
          'causal_quality_bits':CAUSAL_BITS,'causal_invalid_bits':CAUSAL_INVALID_BITS,
          'configuration':read(Path(__file__).parent/'event-portfolio-config.json'),
          'implementation_sha256':code,'native_sources':sources,'baseline_pool_sha256':pool_hashes,
          'baseline_verification':read(baseline/'verification.json'),
          'raw_acquisition_sha256':digest(read(base/'data/acquisition.json')),
          'legacy_method_sha256':digest(read(base/'events/frozen-methodology.json'))}
    freeze(output/'frozen-quality-plan.json',plan)
    pools=annotate_pools(pools,base,20,include_legacy=True)
    report={'plan':plan,'rules':rule_inventory(),'portfolios':[],'rule_effects':[],'past_only_legacy':[],'extremes':[],
            'complete_outcome_counts':{},'primary_result_status':VINTAGE_STATUS,'strategy_parameters_changed':False}
    calendar=read(base/'data/calendar.json')['sessions'];baseline_report=read(baseline/'report.json')
    for strategy,pool in pools.items():
        np.savez_compressed(output/('quality-pool-'+strategy+'.npz'),**pool)
        report['complete_outcome_counts'][strategy]=read(baseline/'catalog'/('pool-%s-20.json'%strategy))['counts']
        total=len(pool['days']);legacy=pool['legacy_feature_reasons']|pool['legacy_outcome_reasons']
        for slots in (5,10):
            allbook=None
            for variant in VARIANTS:
                mask=quality_mask(pool,variant);book,m,indices=selected_book(pool,mask,days,slots)
                # Assert original ALL/VALID results reproduce exactly before attributing differences.
                if variant in ('ALL','VALID_ONLY'):
                    old=next(r for r in baseline_report['portfolios'] if r['strategy']==strategy and r['slots']==slots and r['horizon']==20 and r['quality']==variant and r['costs']=='REALISTIC')
                    for metric in ('end_capital','cagr','max_drawdown'):
                        if not math.isclose(m[metric],old['metrics'][metric],rel_tol=1e-12,abs_tol=1e-10):raise AssertionError('baseline changed '+metric)
                proof,positions=validate_book(book,pool,indices,slots,20,calendar,meta)
                row={'strategy':strategy,'slots':slots,'variant':variant,'metrics':m,'pool_used':int(mask.sum()),
                     'excluded_events':int((~mask).sum()),'total_computable_pool':total,'invariants':proof}
                report['portfolios'].append(row)
                save_book(output/('book-%s-%s-s%d.npz'%(strategy,variant,slots)),book,indices)
                if variant=='ALL':allbook=book
                if strategy=='CONFLUENCE_SETUP_READY' and variant in ('ALL','PIT_SAFE','VALID_ONLY'):
                    used=np.flatnonzero(book[5]);positive=used[book[4][used]>0];negative=used[book[4][used]<0]
                    for side,chosen in [('POSITIVE',positive[np.argsort(book[4][positive])[::-1]][:20]),('NEGATIVE',negative[np.argsort(book[4][negative])][:20])]:
                        for rank,j in enumerate(chosen,1):
                            index=indices[j];ledger=book[7][j]
                            report['extremes'].append({'source_book':variant,'slots':slots,'side':side,'rank':rank,
                                'ticker':meta['universe'][pool['assets'][index]],'event_index':int(index),
                                'signal_day':days[pool['days'][index]],'entry_day':days[int(ledger[0])],'exit_day':days[int(ledger[1])],
                                'net_pnl_usd':float(book[4][j]),'gross_event_return_pct':float((pool['paths'][index,-1]-1)*100),
                                **{q:bool(quality_mask(pool,q)[index]) for q in VARIANTS},
                                'pit_reason_bits':int(pool['pit_reasons'][index]),'legacy_quality':('VALID','SUSPECT','INVALID')[pool['quality'][index]],
                                'flags':flag_detail(pool,index)})
                print('Quality panel',strategy,slots,variant,'pool',mask.sum(),'CAGR',round(m['cagr']*100,4),flush=True)
            previous=allbook[0][0];kept=np.ones(total,dtype=bool)
            previous_metrics=next(p['metrics'] for p in report['portfolios'] if p['strategy']==strategy and p['slots']==slots and p['variant']=='ALL')
            for bit,rule in REASONS.items():
                affected=(legacy&bit)!=0;new=kept&affected
                if affected.any():_,single,_=selected_book(pool,~affected,days,slots)
                else:single=next(p['metrics'] for p in report['portfolios'] if p['strategy']==strategy and p['slots']==slots and p['variant']=='ALL')
                kept &= ~affected
                if new.any():_,cumulative,_=selected_book(pool,kept,days,slots)
                else:cumulative=previous_metrics
                report['rule_effects'].append({'strategy':strategy,'slots':slots,'bit':bit,'rule':rule,
                    'excluded_by_rule':int(affected.sum()),'feature_flagged_events':int(np.count_nonzero(pool['legacy_feature_reasons']&bit)),
                    'outcome_flagged_events':int(np.count_nonzero(pool['legacy_outcome_reasons']&bit)),
                    'future_only_events':int(np.count_nonzero((pool['legacy_feature_reasons']&bit==0)&(pool['legacy_outcome_reasons']&bit!=0))),
                    'affected_all_positions':int(np.count_nonzero(allbook[5].astype(bool)&affected)),
                    'all_realized_pnl_of_affected_positions':float(allbook[4][affected].sum()),
                    'single_rule_metrics':single,'sequential_newly_excluded':int(new.sum()),
                    'sequential_pool_remaining':int(kept.sum()),'sequential_end_capital_delta':float(cumulative['end_capital']-previous),
                    'sequential_metrics':cumulative})
                previous=cumulative['end_capital'];previous_metrics=cumulative
            assert np.array_equal(kept,quality_mask(pool,'VALID_ONLY'))
            expected=next(p for p in report['portfolios'] if p['strategy']==strategy and p['slots']==slots and p['variant']=='VALID_ONLY')['metrics']['end_capital']
            assert math.isclose(previous,expected,rel_tol=1e-12)
            mask=pool['legacy_feature_reasons']==0;book,m,indices=selected_book(pool,mask,days,slots)
            report['past_only_legacy'].append({'strategy':strategy,'slots':slots,'pool_used':int(mask.sum()),'metrics':m})
            write(output/'report.json',report)
    assert fingerprints()==sources
    write(output/'report.json',report);(output/'REPORT.md').write_text(render(report))
    write(output/'extreme-confluence.json',report['extremes'])
    for filename,rows in [('portfolio-comparison.csv',report['portfolios']),('rule-effects.csv',report['rule_effects']),('extreme-confluence.csv',report['extremes'])]:
        flattened=[]
        for row in rows:
            flat={}
            for k,v in row.items():
                if isinstance(v,dict):
                    for a,b in v.items():
                        if not isinstance(b,(dict,list)):flat[k+'_'+a]=b
                elif k=='flags':flat[k]='; '.join(r['flag']+':'+r['knowable_by_entry'] for r in v)
                else:flat[k]=v
            flattened.append(flat)
        with (output/filename).open('w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=sorted(set().union(*(r.keys() for r in flattened))));writer.writeheader();writer.writerows(flattened)
    write(output/'verification.json',{'report_sha256':digest(report),'plan_sha256':digest(plan),'native_sources_unchanged':True,
          'all_and_valid_baselines_reproduced':True,'historical_books_validated':len(report['portfolios']),
          'data_vintage_status':VINTAGE_STATUS,'monte_carlo_executed':False})
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base',required=True);p.add_argument('--baseline',required=True);p.add_argument('--output',required=True)
    args=p.parse_args();run(args.base,args.baseline,args.output)
