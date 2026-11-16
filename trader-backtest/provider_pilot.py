#!/usr/bin/env python3
"""Paired reconstruction counts on the identical frozen Yahoo setup cohort.

Only after a passing free live test; no return, profitability or MC analysis.
"""
import argparse
from collections import Counter
import copy
from pathlib import Path

from common import digest, read, write
from data import MarketData
from engine import cost_scenarios
from intraday_providers import AlpacaProvider, BarCache, ProviderRouter
from provider_collect import collect
from run import fingerprints
from true_trade_collect import freeze
from true_trade_execution import reconstruct, session_rows
from true_trade_replay import STRATEGIES, replay
from true_trade_run import validate


def comparison(baseline, candidate, config, start, end):
    """Use the baseline's exact IDs as denominator, including absent data.

    Native provider-specific validation can still reject an event. Additional
    candidate-provider setups are counted separately and never enter this cohort.
    """
    if (baseline["sessions"] != candidate["sessions"] or baseline["universe"] != candidate["universe"] or
            baseline.get("splits") != candidate.get("splits") or baseline.get("evidence") != candidate.get("evidence") or
            any(baseline["bars"][s].get("1d") != candidate["bars"][s].get("1d") for s in baseline["universe"])):
        raise ValueError("paired pilot requires identical frozen daily, calendar, universe, splits and evidence")
    markets = {"YAHOO": MarketData(baseline), "ALPACA": MarketData(candidate)}
    adapted = {name: replay(market, config, start, end) for name, market in markets.items()}
    cohort = {s: [a for a in adapted["YAHOO"]["audit"] if a["strategy"] == s and a["setup_ready"]] for s in STRATEGIES}
    counts = {}
    for name, native in adapted.items():
        audits = {a["id"]: a for a in native["audit"]}
        plans = {p["id"]: p for p in native["plans"]}
        coverage = {(r["ticker"], r["session"]): r for r in native["coverage"]}
        counts[name] = {}
        for strategy, observations in cohort.items():
            ids = {a["id"] for a in observations}
            available = triggered = true_trades = 0
            exclusions = Counter()
            for original in observations:
                a = audits.get(original["id"], {})
                covered = coverage.get((original["ticker"], original["session"]), {})
                available += bool(covered.get("contiguous_opening_bars"))
                triggered += bool(a.get("triggered"))
                plan = plans.get(original["id"])
                if not plan:
                    exclusions[a.get("reason") or "FROZEN_SETUP_NOT_CONFIRMED_BY_NATIVE_PROVIDER"] += 1
                    continue
                _, rows = session_rows(markets[name], plan["symbol"], plan["session"])
                trade = reconstruct(plan, rows, markets[name].clock, config, cost_scenarios(config)["REALISTIC"], "conservative")
                if trade["true_trade"]:
                    true_trades += 1
                else:
                    exclusions[trade["status"]] += 1
            counts[name][strategy] = dict(setup_ready=len(observations), intraday_available=available,
                trigger_reconstructed=triggered, true_trades=true_trades, exclusions=dict(exclusions),
                additional_native_setups_outside_cohort=sum(a["setup_ready"] and a["id"] not in ids
                    for a in native["audit"] if a["strategy"] == strategy))
    return dict(period=[start, end], universe=baseline["universe"], configuration_sha256=digest(config),
        setup_cohort=cohort, counts=counts, feed="IEX", same_setup_event_ids=True,
        costs="REALISTIC", quality="PIT_SAFE", intrabar_order="conservative",
        interpretation="Reconstruction counts only; no profitability claim; incomplete IEX slots are never filled")


def pilot(document, config, audit, output, start, end):
    validate(config)
    if audit.get("alpaca_free_test", {}).get("status") != "PASSED" or audit["alpaca_free_test"].get("feed") != "IEX":
        raise ValueError("passing free IEX live test required before reconstruction")
    if sorted(config["universe"]) != sorted(document["universe"]):
        raise ValueError("frozen pilot universe must match the existing strategy configuration")
    output = Path(output)
    provider = AlpacaProvider(feed="iex", cache=BarCache(output / "cache"), sessions=document["sessions"])
    if not provider.authenticated:
        raise ValueError("Alpaca environment secrets required; no reconstruction attempted")
    sources = fingerprints()
    freeze(output / "frozen-pilot.json", dict(input_sha256=digest(document), config=config,
        audit_sha256=digest(audit), start=start, end=end, strategy_sources=sources))
    baseline = copy.deepcopy(document)
    for frames in baseline["bars"].values():
        for frame in ("1m", "5m"):
            for row in frames.get(frame, []):
                if row.get("provider", "YAHOO") != "YAHOO":
                    raise ValueError("baseline must be the original frozen Yahoo pilot")
                row.update(provider="YAHOO", feed="UNSPECIFIED", adjustment="split")
    provider_config = dict(priority=["ALPACA"], alpaca_feed="iex", adjustments={"ALPACA": "raw"})
    candidate = collect(baseline, provider_config, output / "alpaca", start, end,
                        router=ProviderRouter([provider], priority=["ALPACA"]))
    report = comparison(baseline, candidate, config, start, end)
    report["strategy_sources_unchanged"] = fingerprints() == sources
    if not report["strategy_sources_unchanged"]:
        raise RuntimeError("strategy source changed")
    write(output / "provider-pilot-comparison.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, help="Original frozen Yahoo pilot dataset")
    parser.add_argument("--config", default=str(Path(__file__).with_name("true-trade-config.json")))
    parser.add_argument("--audit", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    pilot(read(args.dataset), read(args.config), read(args.audit), args.output, args.start, args.end)
