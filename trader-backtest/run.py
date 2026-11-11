#!/usr/bin/env python3
"""Offline, deterministic backtest CLI. No strategy tuning or execution path."""
import argparse
import hashlib
import math
from pathlib import Path
import platform

from adapters import STRATEGIES, replay
from baseline import portfolio_baseline, signal_baseline
from common import ROOT, digest, read, write
from data import MarketData
from engine import cost_scenarios, simulate
from metrics import benchmark, forward_metrics, forward_samples, portfolio_metrics, trade_metrics
from splits import partitions, select_plans, select_samples


def validate(config):
    if config["start"] > config["end"] or config["leverage"] < 1:
        raise ValueError("invalid dates/leverage")
    for key in ("initial_equity", "risk_per_trade", "max_portfolio_risk", "target_r"):
        if type(config[key]) not in (int, float) or not math.isfinite(config[key]) or not 0 < config[key]:
            raise ValueError("positive configuration required: " + key)
    for key in ("max_positions", "max_hold_sessions", "randomizations", "bootstrap_samples", "low_sample_threshold"):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError("positive integer required: " + key)
    if not 0 < config["risk_per_trade"] <= config["max_portfolio_risk"] <= 1:
        raise ValueError("invalid risk fractions")
    if config["ranking"] != ["confluence_level", "RS_PROXY", "liquidity", "symbol"]:
        raise ValueError("ranking is fixed before replay")
    if any(x not in ("equal_weight", "fixed_risk") for x in config["sizing"]):
        raise ValueError("invalid sizing")
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in config["costs"].values()):
        raise ValueError("invalid costs")
    if config["costs"]["stress_spread_multiplier"] <= 1:
        raise ValueError("stress spread must be wider")


def fingerprints():
    # All existing strategy, entry, exit and risk sources are audit dependencies.
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ("trader-scanner", "trader-confluence", "trader-entry", "trader-exit", "trader-risk", "trader-budget")
            for p in sorted((ROOT / folder).glob("*.py"))}


def run(document, config, output):
    validate(config)
    output = Path(output)
    market = MarketData(document)
    if sorted(config["universe"]) != market.universe:
        raise ValueError("dataset and frozen configuration universes differ")
    if config["start"] < min(market.days) or config["end"] > max(market.days):
        raise ValueError("calendar does not cover requested period")
    sources_before = fingerprints()
    write(output / "frozen-config.json", config)
    audit = market.audit(config["start"], config["end"])
    write(output / "data-audit.json", audit)
    print("Replaying unchanged strategy functions", flush=True)
    adapted = replay(market, config)
    if sources_before != fingerprints():
        raise RuntimeError("strategy sources changed during replay")
    write(output / "signals.json", adapted)
    print("Replay:", len(adapted["signals"]), "observations,", len(adapted["plans"]), "plans", flush=True)
    samples = forward_samples(market, adapted["signals"], config["end"])
    write(output / "forward-observations.json", samples)
    signal_periods, windows = partitions(market.timeline(config["start"], config["end"]), config["is_fraction"],
        config["walk_forward"]["train_sessions"], config["walk_forward"]["test_sessions"])
    # Portfolio analysis uses the common intraday coverage, identically for
    # every strategy, never cash-filled years with no historical triggers.
    covered = []
    for sym in market.universe:
        intra = market.bars(sym, "1m") or market.bars(sym, "5m")
        covered.append({r["day"] for r in intra})
    common = set.intersection(*covered) if covered else set()
    trade_days = [d for d in market.timeline(config["start"], config["end"]) if d in common]
    if trade_days:
        # Keep interior gaps in the time axis; missing execution bars are
        # rejected and missing open-position values invalidate net results.
        trade_days = market.timeline(trade_days[0], trade_days[-1])
    trade_periods, trade_windows = partitions(trade_days, config["is_fraction"],
        config["walk_forward"]["train_sessions"], config["walk_forward"]["test_sessions"])
    costs = cost_scenarios(config)
    report = {"schema_version": 1, "configuration": config, "config_sha256": digest(config), "input_sha256": digest(document),
              "python": platform.python_version(), "strategy_source_sha256": sources_before,
              "backtest_source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                           for p in sorted(Path(__file__).parent.glob("*.py"))},
              "audit": audit, "strategy_counts": {}, "signal_periods": signal_periods, "trade_periods": trade_periods,
              "signal_results": {}, "trade_results": {}, "benchmarks": {}, "walk_forward": [],
              "trade_walk_forward": [],
              "methodology": ["Rules and parameters frozen before outcomes; no optimization performed",
              "Signal observations: one per ticker/session/status; overlapping returns are dependent",
              "MINERVINI_FILTER is descriptive only; no artificial entry or portfolio",
              "Qullamaggie CANDIDATE never enters; confluence candidate is descriptive only",
              "SETUP_READY without entry/stop has forward returns only; trades need current confirmed trigger and scanner risk acceptance",
              "Existing entry ORB15, opening-range-low stop, existing exit planner 2R target; execution next bar open; scheduled flat_by",
              "Qullamaggie freezes its native trigger stop; same predeclared R/flat_by exit harness, not a claimed original proprietary exit",
              "Borrow rates/availability unknown unless explicit historical observations; CFD net results unavailable",
              "Signal returns are gross price returns, not total returns or executable fills; excursions start next session",
              "Portfolio date split uses common intraday coverage; its 70/30 dates differ from the longer daily signal study",
              "Outcomes crossing split boundaries purged; each portfolio segment restarts with identical capital",
              "Walk-forward uses fixed parameters in all windows; no fitting in either IS or OOS",
              "Random portfolio is conditional on accepted count, entry schedule and realized holding duration; not an unconditional strategy test",
              "Random draw failures are reported; no best random portfolio selection",
              "No scientific validity, proof of profitability, or causal confluence benefit claimed"]}
    for strategy in STRATEGIES:
        report["strategy_counts"][strategy] = {"signals": sum(s["strategy"] == strategy for s in adapted["signals"]),
                                               "plans": sum(s["strategy"] == strategy for s in adapted["plans"])}
    for label, days in signal_periods.items():
        report["signal_results"][label] = {}
        for strategy in STRATEGIES:
            selected = select_samples([s for s in samples if s["strategy"] == strategy], days)
            report["signal_results"][label][strategy] = {"forward": forward_metrics(selected, config),
                                                        "random_signal": signal_baseline(market, selected, config)}
    for label, days in trade_periods.items():
        report["trade_results"][label] = {}
        report["benchmarks"][label] = {}
        if not days:
            continue
        for name, c in costs.items():
            report["benchmarks"][label][name] = {sym: benchmark(market, sym, days[0], days[-1], c) for sym in ("SPY", "QQQ")}
            for sizing in config["sizing"]:
                scenario = name + "/" + sizing
                report["trade_results"][label][scenario] = {}
                for strategy in STRATEGIES:
                    plans = select_plans([p for p in adapted["plans"] if p["strategy"] == strategy], days, config["max_hold_sessions"])
                    simulation = simulate(market, plans, config, c, sizing, days[0], days[-1])
                    result = {"trades": trade_metrics(simulation, config["low_sample_threshold"]),
                              "portfolio": portfolio_metrics(simulation),
                              "random_portfolio": portfolio_baseline(market, simulation, config, c, sizing, days[0], days[-1])}
                    if not plans:
                        result["portfolio"] = {"flags": ["NO_EXECUTABLE_PLANS"], "return_pct": None, "max_drawdown": None, "exposure": None}
                    report["trade_results"][label][scenario][strategy] = result
                    write(output / "simulations" / label / (name + "-" + sizing + "-" + strategy + ".json"), simulation)
                print("Measured", label, scenario, flush=True)
    for window in windows:
        test = window["test"]
        result = {"train_period": [window["train"][0], window["train"][-1]], "test_period": [test[0], test[-1]], "results": {}}
        for strategy in STRATEGIES:
            selected = select_samples([s for s in samples if s["strategy"] == strategy], test)
            result["results"][strategy] = forward_metrics(selected, config)
        report["walk_forward"].append(result)
    for window in trade_windows:
        result = {"train_period": [window["train"][0], window["train"][-1]], "test_period": [window["test"][0], window["test"][-1]], "results": {}}
        for strategy in STRATEGIES:
            days = window["test"]
            plans = select_plans([p for p in adapted["plans"] if p["strategy"] == strategy], days, config["max_hold_sessions"])
            sim = simulate(market, plans, config, costs["REALISTIC"], "fixed_risk", days[0], days[-1])
            result["results"][strategy] = {"trades": trade_metrics(sim), "portfolio": portfolio_metrics(sim)}
        report["trade_walk_forward"].append(result)
    # Benchmarks for the complete daily signal periods, too.
    report["daily_benchmarks"] = {label: {s: benchmark(market, s, days[0], days[-1], costs["IDEAL"]) for s in ("SPY", "QQQ")}
                                  for label, days in signal_periods.items() if days}
    write(output / "report.json", report)
    from reporting import render
    (output / "REPORT.md").write_text(render(report))
    print("Wrote", output / "REPORT.md", flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    run(read(args.dataset), read(args.config), args.output)
