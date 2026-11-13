#!/usr/bin/env python3
"""Offline portfolio/Monte-Carlo analysis on unchanged event and trigger rules."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import platform
import random
import statistics as st

import numpy as np

from adapters import replay
from common import ROOT, digest, read, write
from data import MarketData
from engine import cost_scenarios
from portfolio_engine import simulate
from portfolio_metrics import metrics
from portfolio_model import FX, validate
from portfolio_quality import audit as quality_audit
from portfolio_monte_carlo import bootstrap_indices, monte_carlo, quantiles, true_trade_sample_status
from portfolio_sources import Timeline, bind_plans, daily_events, forward_events, matrix_events, reconstruct
from run import fingerprints


def frozen(path, value):
    if path.exists() and read(path) != value:
        raise ValueError("frozen input/protocol differs; choose a new output directory: " + str(path))
    write(path, value)


def event_bootstrap(rows, days, config):
    """Separate gross 20-session event-mean distribution, never a sized book."""
    observed = [r for r in rows if r["return_pct"] is not None]
    out = {"measurement": "GROSS_FORWARD_EVENT_MEAN_NOT_PORTFOLIO", "risk_sizing": "UNAVAILABLE_WITHOUT_STOPS",
           "observed_events": len(observed), "missing_events": len(rows) - len(observed), "methods": {}}
    if not observed:
        return out
    values = [r["return_pct"] for r in observed]
    for method in ("TRADE_IID", "SESSION_BLOCK"):
        rng = random.Random(config["seed"])
        estimates = []
        empty = 0
        if method == "SESSION_BLOCK" and len(days) < 2 * max(config["block_sessions"],config["forward_horizon"]):
            out["methods"][method] = {"status": "INSUFFICIENT_TIME_BLOCKS"}
            continue
        groups = {day: [] for day in days}
        for row in observed:
            groups[row["day"]].append(row["return_pct"])
        for _ in range(config["paths"]):
            if method == "TRADE_IID":
                sample = [values[rng.randrange(len(values))] for _ in values]
            else:
                sample = [v for i in bootstrap_indices(rng, len(days), max(config["block_sessions"], config["forward_horizon"])) for v in groups[days[i]]]
            if sample:
                estimates.append(st.mean(sample))
            else:
                empty += 1
        out["methods"][method] = {"status": "AVAILABLE", "paths": config["paths"], "seed": config["seed"],
            "empty_resamples": empty, "mean_event_return_pct_quantiles": quantiles(estimates),
            "note": "Quantiles conditional on nonempty resamples; not terminal wealth, P&L, R, drawdown or success probabilities. Sparse samples and overlapping horizons remain dependent."}
    return out


def comparisons(results):
    """Identical configuration cells only; no forced winner for missing cohorts."""
    groups={}
    for row in results:
        key="%s/%s/%g"%(row["variant"],row["cost_scenario"],row["risk"])
        groups.setdefault(key,[]).append(row)
    out={}
    required={"MINERVINI","BREAKOUT_SETUP_READY","CONFLUENCE_SETUP_READY"}
    for key,rows in groups.items():
        available={r["strategy"]:r for r in rows if r["status"]=="AVAILABLE"}
        missing=sorted(required-set(available))
        if missing:
            out[key]={"status":"INSUFFICIENT_COMPARABLE_TRADE_COHORTS","missing_strategies":missing,
                "available_strategies":sorted(available),"confluence_improvement":None,"winner_compensation":None}
            continue
        def leaders(field,highest=True):
            pairs=[(r["strategy"],r["historical"].get(field)) for r in rows if r["historical"].get(field) is not None]
            if not pairs:
                return []
            best=(max if highest else min)(v for _,v in pairs)
            return [s for s,v in pairs if v==best]
        con=available["CONFLUENCE_SETUP_READY"]["historical"]
        breakout=available["BREAKOUT_SETUP_READY"]["historical"]
        out[key]={"status":"DESCRIPTIVE_COMPARISON","highest_return":leaders("total_return_pct"),
            "highest_return_per_daily_drawdown":leaders("return_over_drawdown"),
            "lowest_intraday_drawdown":leaders("intraday_max_drawdown",False),
            "confluence_minus_breakout_return_pp":con["total_return_pct"]-breakout["total_return_pct"],
            "confluence_minus_breakout_win_rate":con["win_rate"]-breakout["win_rate"],
            "confluence_minus_breakout_average_winner":(con["average_winner"]-breakout["average_winner"]
                if con["average_winner"] is not None and breakout["average_winner"] is not None else None),
            "monte_carlo_risk_by_strategy":{s:{m:{"negative_total_performance":mc["probabilities"]["negative_total_performance"],
                "capital_depletion":mc["probabilities"]["capital_depletion"],"drawdown_gt_20_pct":mc["probabilities"]["drawdown_gt_20_pct"]}
                for m,mc in row["monte_carlo"].items() if mc["status"]=="AVAILABLE"} for s,row in available.items()},
            "note":"Different event dates and reconstructible/accepted cohorts; not a causal filter-effect estimate or optimization. Inspect winner/loser/R statistics before judging lower win-rate compensation."}
    return out


def report_markdown(report):
    c = report["configuration"]
    lines = ["# Portfolio- und Monte-Carlo-Erstlauf", "", "Keine Strategieoptimierung. Keine Orders, kein Broker, kein Commit, kein Push.", "",
        "## Daten und feste Annahmen", "",
        "- Event-Zeitraum: %s bis %s; Universum: %s." % (*report["event_period"], ", ".join(report["universe"])),
        "- Gemeinsamer Intraday-Vergleich: %s." % (" bis ".join(report["portfolio_period"]) if report["portfolio_period"] else "nicht verfügbar"),
        "- Startkapital: %.2f %s; Slots: %d; Gesamtrisiko: %.2f%%; Hebel: 1." % (c["initial_equity"], c["account_currency"], c["max_positions"], 100*c["max_portfolio_risk"]),
        "- Primär: COOLDOWN20; FULL_RESET separat. Seed %d; angefordert %d Pfade je verfügbarem Trade-Szenario und Bootstrap-Methode." % (c["seed"], c["paths"]),
        "- Kosten sind die bestehenden, ausdrücklich modellierten Backtest-Annahmen: %s." % json.dumps(report["cost_assumptions"]),
        "- USD-Kurse werden mit dem letzten vollständig abgeschlossenen historischen EUR/USD-Tageskurs bewertet. Keine angenommene Parität.",
        "- Die ausgewiesenen trader-risk-Regeln bleiben wirksam; nur das feste Dollar-Risiko pro Trade wird durch die angeforderte Equity-Prozentregel ersetzt.",
        "- Keine vollständige historische Identitäts-/Delisting-Datenbank und keine echten Point-in-Time-Kursvintages.", "",
        "Flags: " + ", ".join(report["flags"]), "", "## Historische Portfolios", "",
        "MaxDD in dieser Tabelle verwendet beobachtete Intraday-Marks; die JSON-Metriken enthalten zusätzlich Tages-Drawdowns und alle weiteren Kennzahlen.", "",
        "| Event | Strategie | Risiko | Kosten | Trades | Endkapital | Return | MaxDD | Status |",
        "|---|---|---:|---|---:|---:|---:|---:|---|"]
    fmt = lambda x, pct=False: "n/v" if x is None else ("%.2f%%" % (x*100) if pct else "%.2f" % x)
    for row in report["results"]:
        m = row["historical"]
        lines.append("| %s | %s | %.2f%% | %s | %d | %s | %s | %s | %s |" % (
            row["variant"], row["strategy"], row["risk"]*100, row["cost_scenario"], m.get("trades",0),
            fmt(m.get("end_capital")), "n/v" if m.get("total_return_pct") is None else "%.2f%%" % m["total_return_pct"],
            fmt(m.get("intraday_max_drawdown"), True), row["status"]))
    lines += ["", "## Empirisches Portfolio-Monte-Carlo", "",
        "IID verschiebt vollständige beobachtete Trades auf die historische Entry-Folge. Moving Blocks erhalten ganze Sitzungen, gleichzeitige Trades und Leertage. Beide wenden Sizing, Kapital-, Ticker-, Slot- und Risk-Grenzen erneut an. Ergebnisse sind bedingt auf die kleine beobachtete Trade-Stichprobe.", "",
        "| Event / Strategie / Risiko | Methode | Pfade | Median Kapital | P05 | P95 | P(Verlust) | DD P95 | längste Verlustserie |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in report["results"]:
        for method, mc in row["monte_carlo"].items():
            if mc.get("status") != "AVAILABLE":
                continue
            lines.append("| %s / %s / %.2f%% | %s | %d | %.2f | %.2f | %.2f | %s | %s | %d |" % (
                row["variant"],row["strategy"],row["risk"]*100,method,mc["completed_paths"],mc["median_end_capital"],
                mc["end_capital_quantiles"]["P05"],mc["end_capital_quantiles"]["P95"],
                fmt(mc["probabilities"]["negative_total_performance"],True),fmt(mc["drawdown_quantiles"]["P95"],True),mc["sequence_risk"]["worst_simulated_loss_streak"]))
    lines += ["", "### Primärer Vergleich: Kapital-Quantile", "",
        "Alle Werte in der Kontowährung. Nur Szenarien mit tatsächlich beobachteten, zugelassenen Trades.", "",
        "| Strategie / Risiko / Kosten | Bootstrap | Mean | P01 | P05 | P10 | P25 | P50 | P75 | P90 | P95 | P99 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for row in report["results"]:
        if row["variant"] != c["primary_variant"]:
            continue
        for method,mc in row["monte_carlo"].items():
            if mc.get("status") == "AVAILABLE":
                label="%s / %.2f%% / %s" % (row["strategy"],row["risk"]*100,row["cost_scenario"])
                lines.append("| %s | %s | %s | %s |" % (label,method,fmt(mc["mean_end_capital"]),
                    " | ".join(fmt(mc["end_capital_quantiles"]["P%02d"%q]) for q in (1,5,10,25,50,75,90,95,99))))
    lines += ["", "### Primärer Vergleich: Drawdown und Gewinnschwellen", "",
        "Drawdown-Wahrscheinlichkeiten beziehen sich auf beobachtete Intraday-Marks. +100% entspricht Kapitalverdopplung. Schwellen strikt > bei Drawdown, >= bei Gewinnen.", "",
        "| Strategie / Risiko / Kosten | Bootstrap | P(Verlust) | DD >5% | DD >10% | DD >20% | DD >30% | Gewinn >=25% | >=50% | >=100% |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for row in report["results"]:
        if row["variant"] != c["primary_variant"]:
            continue
        for method,mc in row["monte_carlo"].items():
            if mc.get("status") == "AVAILABLE":
                prob=mc["probabilities"]
                keys=["negative_total_performance"]+["drawdown_gt_%d_pct"%v for v in (5,10,20,30)]+["gain_ge_%d_pct"%v for v in (25,50,100)]
                lines.append("| %s / %.2f%% / %s | %s | %s |" % (row["strategy"],row["risk"]*100,row["cost_scenario"],method,
                    " | ".join(fmt(prob[k],True) for k in keys)))
    lines += ["", "### Primärer Vergleich: Reihenfolgerisiko", "",
        "| Strategie / Risiko / Kosten | Bootstrap | DD P05 | DD P50 | DD P95 | DD P99 | schlechteste Verlustserie | Median maximale Serie | P95 maximale Serie |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in report["results"]:
        if row["variant"] != c["primary_variant"]:
            continue
        for method,mc in row["monte_carlo"].items():
            if mc.get("status") == "AVAILABLE":
                seq=mc["sequence_risk"]
                lines.append("| %s / %.2f%% / %s | %s | %s | %d | %.2f | %.2f |" % (
                    row["strategy"],row["risk"]*100,row["cost_scenario"],method,
                    " | ".join(fmt(mc["drawdown_quantiles"][k],True) for k in ("P05","P50","P95","P99")),
                    seq["worst_simulated_loss_streak"],seq["median_max_loss_streak"],seq["p95_max_loss_streak"]))
    lines += ["", "Alle weiteren Metriken und Quantile stehen in report.json.", "",
        "## Fehlende Trades und getrennte Forward-Returns", "",
        "Ein Setup-Event ohne belegten nachfolgenden Trigger und vollständigen Preisverlauf hat kein belastbares Risiko-Portfolio. Cash-Endkapital wird in diesem Fall nicht als erfolgreiches Backtestergebnis ausgegeben. Kein Stop und kein Trade werden erfunden.", "",
        "| Event | Strategie | Events | beobachtete 20-Session-Returns | Mean | Median |",
        "|---|---|---:|---:|---:|---:|"]
    for key, f in report["forward_results"].items():
        lines.append("| %s | %s | %d | %d | %s | %s |" % (*key.split("/"),f["events"],f["observed"],fmt(f["mean_pct"]),fmt(f["median_pct"])))
    lines += ["", "Forward-Bootstraps verwenden ebenfalls die angeforderte Pfadzahl, messen aber ausschließlich den mittleren Brutto-Event-Return. Sie liefern kein EUR-Endkapital, kein R und keine risikospezifischen Portfolio-Wahrscheinlichkeiten.", "",
        "## Strategievergleich", "", report["comparison_conclusion"], "",
        "Ein größerer Resampling-Lauf ersetzt keine fehlenden historischen Trades. Selbst 10.000 Pfade aus einem einzelnen Trade enthalten nur einen beobachteten Trade-Ausgang. Keine Gewinnerstrategie wird erzwungen.", "",
        "## Reproduzierbarkeit und Grenzen", "",
        "frozen-protocol.json, verification.json, source-events.json, native-replay.json, event-trade-coverage.json und die einzelnen simulation-*.json sichern Regeln, Quellen, fehlende Daten, Rejections und Equity Curves. Historische Rohdaten und Berichte bleiben ignorierte lokale Run-Artefakte.", "",
        "Prop-Challenge-Regeln sind als separates generisches Modul vorbereitet und wurden nicht als Hauptanalyse ausgeführt.", ""]
    return "\n".join(lines)


def run(dataset, native_config, config, output, fx_result=None, analysis=None, events=None, cache=None, raw_daily=None):
    effective_risk=validate(config)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    before = fingerprints()
    simulator_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(__file__).parent.glob("portfolio*.py"))}
    raw_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(raw_daily).glob("*-1d.json"))} if raw_daily else {}
    protocol = {"configuration": config, "native_configuration": native_config, "dataset_sha256": digest(dataset),
        "fx_sha256": digest(fx_result), "strategy_sha256": before,
        "simulator_sources_sha256":simulator_hashes,"raw_daily_sha256":raw_hashes,
        "effective_risk_rules":effective_risk,"per_trade_risk_override":"equity*risk_fraction converted to quote USD; replaces the fixed USD per-trade ceiling",
        "event_methodology_sha256": digest(read(Path(__file__).parent / "event-methodology.json")),
        "binding": "First confirmed post-event trigger before full reset/unknown/new event; uninterrupted intraday prefix since event required; never retry risk rejections",
        "exits": "Existing native stop/target and conservative exit_price; original target_r/max_hold_sessions/flat_by unchanged",
        "ranking": config["ranking"], "bootstrap": "Full observed admitted trade paths; IID and whole-session moving blocks; shared seed across risk levels",
        "costs": "Existing config.first cost allowances, not observed historical transaction costs"}
    frozen(output / "frozen-protocol.json", protocol)
    market, fx = MarketData(dataset), FX(config["account_currency"], fx_result)
    write(output/"market-data-audit.json",market.audit(native_config["start"],native_config["end"]))
    if sorted(native_config["universe"]) != market.universe:
        raise ValueError("native configuration and execution dataset universes differ")
    if config.get("prop_challenge") is not None:
        raise ValueError("run generic challenges separately; main portfolio analysis never applies challenge rules")
    def cached(name, build):
        path = Path(cache) / name if cache else output / name
        if path.exists():
            value = read(path)
            if (value.get("dataset_sha256") != digest(dataset) or value.get("native_config_sha256") != digest(native_config)
                    or value.get("strategy_sha256") != before):
                raise ValueError("cache provenance mismatch: " + str(path))
        else:
            value = build()
            value.update(dataset_sha256=digest(dataset), native_config_sha256=digest(native_config), strategy_sha256=before)
            value = dict(value)
            for k in ("states", "eligible"):
                if k in value:
                    value[k] = value[k].tolist()
            write(path, value)
        write(output / name, value)
        if "states" in value:
            value["states"], value["eligible"] = np.asarray(value["states"],dtype="uint8"), np.asarray(value["eligible"],dtype=bool)
        return value
    source = cached("source-events.json", lambda: matrix_events(analysis, events, market) if analysis and events else daily_events(market, native_config["start"],native_config["end"]))
    if raw_daily:
        quality_audit(source,raw_daily,config["forward_horizon"],native_config["end"],output/"source-quality-audit.json")
    adapted = cached("native-replay.json", lambda: replay(market, native_config))
    covered = [{r["day"] for r in (market.bars(s,"1m") or market.bars(s,"5m"))} for s in market.universe]
    common = sorted(set.intersection(*covered)) if covered else []
    common = [d for d in common if native_config["start"] <= d <= native_config["end"]]
    timeline = Timeline(market, common[0], common[-1]) if common else Timeline(market,"9999-01-01","9999-01-01")
    scenarios = cost_scenarios(native_config)
    report = {"configuration":config,"event_period":[native_config["start"],native_config["end"]],
        "portfolio_period":[common[0],common[-1]] if common else [], "universe":market.universe,
        "flags":sorted(set(market.flags + ["SURVIVORSHIP_BIAS_RISK", "NO_HISTORICAL_SECURITY_IDENTIFIER_ARCHIVE",
            "NO_TRUE_POINT_IN_TIME_DATA", "CURRENT_SELECTED_UNIVERSE_NOT_HISTORICAL_MEMBERSHIP", "MODELLED_COSTS_NOT_OBSERVED_HISTORICAL_COSTS"])),
        "cost_assumptions":scenarios,"results":[],"forward_results":{}}
    report["effective_risk_rules"]=effective_risk
    report["per_trade_risk_override"]=protocol["per_trade_risk_override"]
    if not raw_daily:
        report["flags"].append("RAW_SOURCE_QUALITY_NOT_RECHECKED")
    coverage = {}
    for variant in config["event_variants"]:
        linked, missing = bind_plans(source, adapted["plans"], variant, market)
        in_period = [p for p in linked if common and common[0] <= p["day"] <= common[-1]]
        templates, failures = reconstruct(market,in_period,native_config,fx,timeline)
        coverage[variant] = {"event_count":len(source["events"][variant]), "linked":len(linked),
            "reconstructed":len(templates), "missing_trigger_plans":missing,"reconstruction_failures":failures,
            "native_plan_rejections":adapted["rejections"]}
        forwards = forward_events(market,source["events"][variant],config["forward_horizon"],native_config["end"])
        write(output / ("forward-events-"+variant+".json"),forwards)
        for strategy in config["strategies"]:
            fr = [r for r in forwards if r["strategy"] == strategy]
            values = [r["return_pct"] for r in fr if r["return_pct"] is not None]
            study_days=market.timeline(native_config["start"],native_config["end"])
            report["forward_results"][variant+"/"+strategy] = {"events":len(fr),"observed":len(values),
                "mean_pct":st.mean(values) if values else None,"median_pct":st.median(values) if values else None,
                "bootstrap":event_bootstrap(fr,study_days,config)}
            if raw_daily:
                valid=[r["return_pct"] for r in fr if r["return_pct"] is not None and r.get("quality")=="VALID"]
                report["forward_results"][variant+"/"+strategy].update(quality_counts=dict(Counter(r["quality"] for r in fr)),
                    retrospective_valid_only={"observed":len(valid),"mean_pct":st.mean(valid) if valid else None,
                                             "median_pct":st.median(valid) if valid else None})
            for scenario in config["cost_scenarios"]:
                costs=scenarios[scenario]
                for risk in config["risk_per_trade"]:
                    selected=[p for p in templates if p["strategy"] == strategy]
                    book=simulate(selected,timeline,config,costs,risk)
                    enough=len(book["trades"])>=30
                    row={"variant":variant,"strategy":strategy,"risk":risk,"aggressive":risk>0.01,"cost_scenario":scenario,
                        "measurement":"TRUE_TRADE_PORTFOLIO","sample_status":true_trade_sample_status(len(book["trades"])),
                        "reconstructible_candidates":len(selected), "rejections":dict(Counter(r["reason"] for r in book["rejections"])),
                        "status":("AVAILABLE" if enough else "VERY_LOW_SAMPLE") if book["trades"] else "INSUFFICIENT_TRADE_DATA",
                        "historical":metrics(book,timeline) if enough else {"trades":len(book["trades"]),"end_capital":None,"total_return_pct":None,"intraday_max_drawdown":None},
                        "technical_function_test":metrics(book,timeline) if book["trades"] and not enough else None,
                        "monte_carlo":{method:monte_carlo(book["accepted_templates"],timeline,config,costs,risk,method) for method in ("TRADE_IID","SESSION_BLOCK")}}
                    report["results"].append(row)
                    name="simulation-%s-%s-%s-%g.json" % (variant,strategy,scenario,risk)
                    write(output/name,{k:v for k,v in book.items() if k!="accepted_templates"})
                    write(output/"report.json",report)
                    print(variant,strategy,scenario,risk,"trades",len(book["trades"]),"MC",[r["completed_paths"] for r in row["monte_carlo"].values()],flush=True)
    report["strategy_comparisons"]=comparisons(report["results"])
    report["comparison_conclusion"] = "Dieser Pilot erlaubt keinen belastbaren Rendite-/Drawdown-Sieger und keinen Nachweis, dass Confluence Breakout verbessert oder eine niedrigere Trefferquote durch größere Gewinner ausgleicht. Fehlende historische Entries/Stops und kleine, unterschiedlich rekonstruierbare Stichproben begrenzen den Vergleich. Alle Szenarien verwenden dieselben vorab festgelegten Portfolio- und Kostenregeln."
    if any(v["status"]=="DESCRIPTIVE_COMPARISON" for v in report["strategy_comparisons"].values()):
        report["comparison_conclusion"]="Deskriptive Vergleiche bei identischen Parametern stehen in strategy_comparisons in report.json. Unterschiedliche Event-Zeitpunkte und rekonstruierbare Stichproben erlauben keinen kausalen Nachweis eines Confluence-Vorteils. Es wurde keine Gewinnerstrategie erzwungen oder optimiert."
    write(output/"event-trade-coverage.json",coverage)
    write(output/"report.json",report)
    (output/"REPORT.md").write_text(report_markdown(report))
    if before != fingerprints():
        raise RuntimeError("existing strategy/risk sources changed")
    write(output/"verification.json",{"protocol_sha256":digest(protocol),"report_sha256":digest(report),
        "python":platform.python_version(),"numpy":np.__version__,"strategy_sources_unchanged":True,
        "simulator_sources_sha256":simulator_hashes,
        "input_sha256":digest(dataset),"fx_sha256":digest(fx_result)})
    return report


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset",required=True)
    p.add_argument("--native-config",default=str(Path(__file__).parent/"config.first.json"))
    p.add_argument("--config",default=str(Path(__file__).parent/"portfolio-config.json"))
    p.add_argument("--fx-raw")
    p.add_argument("--analysis")
    p.add_argument("--events")
    p.add_argument("--cache")
    p.add_argument("--raw-daily",help="Original collect.py raw directory; optional uniform retrospective QC")
    p.add_argument("--output",required=True)
    p.add_argument("--capital",type=float)
    p.add_argument("--max-positions",type=int)
    p.add_argument("--max-portfolio-risk",type=float)
    p.add_argument("--risks",type=float,nargs="+")
    p.add_argument("--cost-scenarios",nargs="+",choices=("IDEAL","REALISTIC","STRESS"))
    p.add_argument("--include-aggressive",action="store_true")
    p.add_argument("--paths",type=int)
    p.add_argument("--seed",type=int)
    a=p.parse_args()
    if bool(a.analysis) != bool(a.events):
        p.error("--analysis and --events must be supplied together")
    fxraw=read(a.fx_raw)["chart"]["result"][0] if a.fx_raw else None
    config=read(a.config)
    for key, value in (("initial_equity",a.capital),("max_positions",a.max_positions),
        ("max_portfolio_risk",a.max_portfolio_risk),("risk_per_trade",a.risks),
        ("cost_scenarios",a.cost_scenarios),("paths",a.paths),("seed",a.seed)):
        if value is not None:
            config[key]=value
    if a.include_aggressive:
        config["risk_per_trade"]=sorted(set(config["risk_per_trade"]+config["aggressive_risk_scenarios"]))
    run(read(a.dataset),read(a.native_config),config,a.output,fxraw,a.analysis,a.events,a.cache,a.raw_daily)
