#!/usr/bin/env python3
"""Offline True Trade run: native reconstruction, six sensitivities, gated risk/MC."""
import argparse
from collections import Counter
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path

from common import digest, read, write
from data import MarketData
from engine import cost_scenarios
from run import fingerprints
from true_trade_collect import freeze
from true_trade_execution import reconstruct, session_rows, trade_metrics
from true_trade_replay import STRATEGIES, replay


def validate(config):
    original = read(Path(__file__).parent / "config.first.json")
    for key in ("target_r", "flat_by", "max_hold_sessions", "costs"):
        if config[key] != original[key]:
            raise ValueError("existing exit/cost assumptions must remain unchanged: " + key)
    if config["primary_quality"] != "PIT_SAFE" or config["primary_costs"] != "REALISTIC" or config["primary_intrabar_order"] != "conservative":
        raise ValueError("PIT_SAFE/REALISTIC/conservative primary required")
    if (config["leverage"] != 1 or not 1 <= config["max_positions"] <= 5 or
            not 0 < config["max_portfolio_risk"] <= .03 or config["risk_per_trade"] != [.0025,.005,.01]):
        raise ValueError("requested unlevered portfolio risk protocol required")
    if (type(config["paths"]) is not int or config["paths"] < 10000 or
            config["minimum_monte_carlo_trades"] < 100 or
            type(config["block_sessions"]) is not int or config["block_sessions"] < 1):
        raise ValueError("100 trades / 10000 paths and valid block size required")
    if not math.isfinite(config["initial_equity"]) or config["initial_equity"] <= 0:
        raise ValueError("positive initial equity required")


def markdown(report, trades):
    lines = ["# Historische True-Trade-Rekonstruktion", "",
        "Primär: PIT_SAFE, REALISTIC, conservative. INTRADAY_HISTORY_LIMITED. Keine Strategieoptimierung.", "",
        "Zeitraum: %s bis %s. Universum: %s (bestehender Acht-Aktien-Pilot, kein Gesamtmarkt)." %
        (*report["period"], ", ".join(report["universe"])),
        "Daten mit mindestens einem vollständigen regulären Intraday-Tag: %d Aktien. %d vollständige Aktien-Sessions." %
        (report["coverage"]["sufficient_stocks"], report["coverage"]["complete_stock_sessions"]), "",
        "NOT_HISTORICALLY_PIT_CERTIFIED: heutige revidierbare Yahoo-Snapshots mit angenommener Bar-Close-Verfügbarkeit; kein historischer Liefernachweis.", "",
        "| Strategie | Kandidaten | SETUP_READY | TRIGGERED | True Trades | Sample |",
        "|---|---:|---:|---:|---:|---|"]
    for name in STRATEGIES:
        s = report["strategies"][name]
        lines.append("| %s | %d | %d | %d | %d | %s |" %
            (name, s["candidates"], s["setup_ready"], s["triggered"], s["metrics"]["trades"], s["metrics"]["sample_status"]))
    lines += ["", "Zähleinheit: tägliche Kandidaten-/SETUP_READY-Beobachtung, höchstens erster nativer Trigger je Strategie/Ticker/Session. Keine Behauptung unabhängiger FULL_RESET-/COOLDOWN-Ereignisse.", "",
              "Confluence: Minervini PASS am vorherigen abgeschlossenen Daily-Close + Breakout SETUP_READY → derselbe reale Qullamaggie-Trigger; Minervini erzeugt keinen Entry.", ""]
    for name in STRATEGIES:
        s = report["strategies"][name]
        lines += ["## " + name, "", "Screening: " + str(s["screening_reasons"]), "",
                  "Ausschlüsse innerhalb Kandidat/SETUP_READY: " + str(s["exclusions"]), "",
                  "True-Trade-Metriken (USD pro Aktie; Win Rate als Anteil):", "", "```json", json.dumps(s["metrics"], indent=2), "```", ""]
    lines += ["## Beispieltrades", "", "| Ticker | Datum | Strategie / Trigger | Entry UTC / Preis | Stop | Exit UTC / Preis / Grund | R netto | P&L netto/Aktie |",
              "|---|---|---|---|---:|---|---:|---:|"]
    for t in trades[:12]:
        lines.append("| %s | %s | %s / %s | %s / %.4f | %.4f | %s / %.4f / %s | %.3f | %.4f |" %
            (t["ticker"], t["session"], t["strategy"], t["trigger_type"], t["entry_time"], t["entry_price"],
             t["stop_initial"], t["exit_time"], t["exit_price"], t["exit_reason"], t["r_multiple"], t["net_pnl"]))
    if not trades:
        lines += ["", "Keine vollständigen True Trades: keine Beispiele synthetisiert."]
    lines += ["", "## Ausführung und Grenzen", "",
        "ORH/ORL existiert erst nach Range-Ende; bestehende Close-Bestätigung auf einer späteren Bar. Entry ausschließlich nächstes zusammenhängendes Bar-Open. Keine Daily-Ersatzausführung.", "",
        "Initialer Stop: Session-Low/-High ausschließlich bis Trigger einschließlich; VWAP_FAILURE: zum Trigger bekannter VWAP-Reclaim-Level als fester initialer Stop. Rundung und 2R-Target aus bestehendem trader-exit am tatsächlichen Entry-Open. Keine späteren Tagesextreme. Flat-by 15:55 ET, bei frühem Schluss spätestens Close minus fünf Minuten. Native automatische Trailing-Exits sind nicht definiert; keine neue Trailing-Regel ergänzt.", "",
        "Stop/Target-Zeiten innerhalb einer OHLC-Bar bleiben Intervalle. Beide Levels innerhalb derselben Bar: AMBIGUOUS_INTRABAR_ORDER, primär Stop zuerst; optimistic separat. Eindeutiges Stop-/Target-Open hat Vorrang. Keine günstige Target-Gap-Verbesserung.", "",
        "Kosten: unveränderte Pilotannahmen (REALISTIC: 10 bp voller Spread, 5 bp Slippage je Seite, 0,01 USD Gebühr je Aktie/Roundtrip). IDEAL null; STRESS doppelter Spread und doppelte Slippage. Keine gemessenen historischen Kosten. Short-P&L ohne unbekannte Borrow-/Locate-Kosten separat gekennzeichnet.", "",
        "Unvollständige Exits bleiben als DATA_END/INSUFFICIENT_EXIT_DATA im Audit; keine Nullrenditen. Vollständige-Trade-Metriken können durch solche fehlenden Outcomes bedingt sein.", "",
        "Portfolio: 0,25/0,50/1,00 % Stop-Risiko, maximal fünf Positionen, maximal 3 % initiales Gesamtrisiko, Cash-/Exposure-Cap ohne Hebel. Unter 100 Trades nur technischer LOW_SAMPLE/VERY_LOW_SAMPLE-Test. Strategie-Monte-Carlo ausschließlich ab 100 Trades, mindestens 10.000 IID- und, bei ausreichenden Sessions, Block-Pfade.", "",
        "Keine Aussage über reale Profitabilität; keine Extrapolation auf 1993–2026. Aktuelles Überlebenden-Universum, kleine Rangpopulation, keine historische Identitäts-/Delisting- und Catalyst-Vintage-Datenbank. Keine manuelle Tickerentfernung.", "",
        "Für mehrere Jahre erforderlich: lizenzierte, lückenarme historische US-1m-OHLCV einschließlich korrigierbarer Vintages/Splits, regulärer/erweiterter Sessions und delisteter Titel; zusätzlich zeitgestempelte Earnings-/Guidance-/Catalyst-Archive sowie für Ausführung NBBO/Spreads und für Shorts Borrow/Locates. Anbieterberechtigung und genaue Reichweite müssen separat verifiziert werden.", "",
        "## Portfolio-/Monte-Carlo-Verfügbarkeit", "", "```json", json.dumps(report["risk_research"], indent=2), "```", ""]
    return "\n".join(lines)


def run(document, config, output):
    validate(config)
    output = Path(output)
    market = MarketData(document)
    if sorted(config["universe"]) != market.universe:
        raise ValueError("frozen universe mismatch")
    if config["max_hold_sessions"] != 1 or config["primary_quality"] != "PIT_SAFE":
        raise ValueError("v1 reconstructs existing one-session PIT_SAFE policy only")
    before = fingerprints()
    freeze(output / "frozen-config.json", config)
    freeze(output / "frozen-input.json", {"input_sha256": digest(document), "strategy_sources": before})
    days = sorted({r["day"] for s in market.universe for f in ("1m", "5m") for r in market.bars(s, f)
                   if r["_end"] <= market.clock.close(r["day"])})
    if not days:
        raise ValueError("no actual intraday observations; no daily fallback")
    adapted = replay(market, config, days[0], days[-1])
    write(output / "trigger-replay.json", adapted)
    sensitivities, primary, all_rows = {}, [], []
    statuses = {}
    for order in ("conservative", "optimistic"):
        for scenario, costs in cost_scenarios(config).items():
            rows = []
            for plan in adapted["plans"]:
                _, bars = session_rows(market, plan["symbol"], plan["session"])
                result = reconstruct(plan, bars, market.clock, config, costs, order)
                result["cost_scenario"] = scenario
                rows.append(result)
                if order == "conservative" and scenario == "REALISTIC":
                    statuses[plan["id"]] = result["status"]
            all_rows.extend(rows)
            sensitivities[order + "/" + scenario] = {s: trade_metrics([r for r in rows if r["strategy"] == s]) for s in STRATEGIES}
            if order == "conservative" and scenario == "REALISTIC":
                primary = [r for r in rows if r.get("true_trade")]
    write(output / "trade-reconstruction.json", all_rows)
    # Human-readable complete ledger, no large artifacts in tracked paths.
    fields = ["id", "ticker", "strategy", "setup_date", "trigger_type", "trigger_time", "entry_time", "entry_price",
        "stop_initial", "stop_basis", "stop_known_at", "target_initial", "exit_time", "exit_price", "exit_reason", "side",
        "spread", "slippage", "fees", "costs", "gross_pnl", "net_pnl", "r_multiple", "holding_minutes",
        "pit_safe_status", "data_quality_status", "catalyst_status", "ambiguity_flags", "cost_scenario", "intrabar_order"]
    with gzip.open(output / "true-trades.csv.gz", "wt", newline="") as stream:
        writer = csv.DictWriter(stream, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(r for r in all_rows if r.get("true_trade"))
    summaries = {}
    for s in STRATEGIES:
        observations = [r for r in adapted["audit"] if r["strategy"] == s]
        candidates = [r for r in observations if r["candidate"] or r["setup_ready"]]
        exclusions = Counter(r["reason"] or statuses.get(r["id"], "UNKNOWN") for r in candidates
                             if r["reason"] or statuses.get(r["id"]) != "TRUE_TRADE")
        summaries[s] = {"candidates": sum(r["candidate"] for r in observations),
            "screened_stock_sessions": len(observations),
            "screening_reasons": dict(Counter(r["reason"] or statuses.get(r["id"], "UNKNOWN") for r in observations)),
            "setup_ready": sum(r["setup_ready"] for r in observations),
            "triggered": sum(r["triggered"] for r in observations), "exclusions": dict(exclusions),
            "metrics": sensitivities["conservative/REALISTIC"][s]}
    coverage = adapted["coverage"]
    from true_trade_portfolio import research
    risk = research(primary, market, config, days[0], days[-1], output / "portfolio")
    report = {"period": [days[0], days[-1]], "universe": market.universe,
        "configuration_sha256": digest(config), "strategy_sources_unchanged": fingerprints() == before,
        "flags": ["INTRADAY_HISTORY_LIMITED", "NOT_HISTORICALLY_PIT_CERTIFIED", "SURVIVORSHIP_BIAS_RISK"],
        "coverage": {"sufficient_stocks": len({r["ticker"] for r in coverage if r["complete_session"]}),
            "complete_stock_sessions": sum(r["complete_session"] for r in coverage),
            "stock_sessions_with_opening_prefix": sum(bool(r["contiguous_opening_bars"]) for r in coverage),
            "stocks_with_any_intraday": len({r["ticker"] for r in coverage if r["regular_bars"]})},
        "strategies": summaries, "sensitivities": sensitivities, "risk_research": risk,
        "invalid_input_rows": market.issues,
        "implementation_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob("true_trade*.py")}}
    if not report["strategy_sources_unchanged"]:
        raise RuntimeError("existing strategies changed")
    write(output / "summary.json", report)
    (output / "REPORT.md").write_text(markdown(report, primary))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    run(read(args.dataset), read(args.config), args.output)
