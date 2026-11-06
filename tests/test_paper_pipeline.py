"""Synthetic, offline end-to-end stock short and CFD paper studies.

OHLC bars and contract terms are constructed here, never presented as market
data. The real scanner, entry, exit, budget, risk and sentiment code runs.
"""

import copy
import datetime as dt
import io
import json
import os
import socket
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from _load import load

entry = load("trader-entry", "entry")
exitp = load("trader-exit", "exit")
scanner = load("trader-scanner", "scan")
budget = load("trader-budget", "size")
risk = load("trader-risk", "check")
exposure = load("trader-risk", "exposure")
sentiment = load("trader-sentiment", "sentiment")
NOW = dt.datetime(2026, 10, 19, 14, 15)
STAMP = "2026-10-19T14:15:00Z"
DAY = "2026-10-19"


def run(module, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        status = module.main(argv)
    return status, out.getvalue(), err.getvalue()


def config(**extra):
    result = dict(budget.DEFAULTS, budget=5000, paper=True, require_stop=True,
                  allow_short=True, allow_margin=True, allow_cfd=True)
    result.update(extra)
    return result


def contract(**extra):
    result = {"verified": True, "source": "SYNTHETIC contract terms, not a broker connection",
              "underlying_type": "stock", "underlying_symbol": "TSLA", "calendar": "NYSE",
              "contract_size": 10, "qty_step": 0.1, "min_qty": 0.1, "tick_size": 0.01,
              "margin_rate": 0.2, "commission_per_contract": 0.5,
              "financing_reserve_per_contract": 0.25, "slippage": 0.02,
              "guaranteed_stop": False, "guaranteed_stop_fee_per_contract": 0}
    result.update(extra)
    return result


def model(side="buy", kind="stock", **extra):
    result = {"created_by": "trader-exit", "created_at": STAMP, "date": DAY, "symbol": "TSLA",
              "side": side, "instrument_type": kind, "currency": "USD", "entry": 100,
              "stop": 102 if side == "sell" else 98, "target": 96 if side == "sell" else 104,
              "spread": 0.1, "price_basis": "mid", "leverage": 1, "analysis_only": True,
              "setup": "orb", "flat_by": "15:55"}
    if kind == "cfd":
        result.update(symbol="TSLA-CFD-1", contract=contract(), leverage=5)
    result.update(extra)
    return result


def proposal(plan, qty):
    return {"symbol": plan["symbol"], "side": plan["side"], "instrument_type": plan["instrument_type"],
            "analysis_only": True, "paper": True, "type": "market", "qty": str(qty), "plan": copy.deepcopy(plan)}


def check(orders, cfg=None, portfolio=None, prices=None, times=None, state=None):
    return risk.check(orders, cfg or config(), portfolio or {"cash": 5000, "positions": []},
                      prices=prices, state=state or {"orders": []}, now=NOW, price_times=times)


def bars_document(side="buy", symbol="TSLA"):
    closes = [100, 100, 100, 100, 100, 100, 100.5, 101, 102]
    quotes = {key: [] for key in ("open", "high", "low", "close", "volume")}
    for index, close in enumerate(closes):
        high, low = (101, 99) if index == 0 else (close + 0.2, close - 0.2)
        if side == "sell":
            close, high, low = 200 - close, 200 - low, 200 - high
        for key, value in (("open", close), ("high", high), ("low", low), ("close", close), ("volume", 10000)):
            quotes[key].append(value)
    start = int(dt.datetime(2026, 10, 19, 13, 30, tzinfo=dt.timezone.utc).timestamp())
    return {"chart": {"result": [{"meta": {"symbol": symbol, "regularMarketTime": start + 9 * 300,
                                           "previousClose": 100, "note": "SYNTHETIC OHLC bars"},
                                   "timestamp": [start + index * 300 for index in range(9)],
                                   "indicators": {"quote": [quotes]}}], "error": None}}


def full_document(side="buy", kind="stock"):
    """Build evidence from actual modules, including the CLI-generated exit."""
    instrument = model(side, kind)
    symbol = instrument["symbol"]
    raw = json.dumps(bars_document(side, symbol))
    chart = entry.parse_chart(raw, symbol)
    checked_entry = entry.public(entry.evaluate(chart, NOW, side=side, instrument=instrument if kind == "cfd" else None))
    series = scanner.parse_chart(raw, symbol)
    cutoff = dt.datetime(2026, 10, 19, 10, 15)
    hits, counts, failed = scanner.scan_orb({symbol: series}, [symbol], NOW.date(), cutoff, 15, side=side)
    assert not failed and counts["hits"] == 1
    cfd_scan = None
    with tempfile.TemporaryDirectory() as directory:
        descriptor = os.path.join(directory, "instrument.json")
        with open(descriptor, "w") as handle:
            json.dump(instrument, handle)
        args = ["plan", "--symbol", symbol, "--side", side, "--entry", str(checked_entry["trigger"]["price"]),
                "--or-high", "101", "--or-low", "99", "--spread", "0.1", "--now", "2026-10-19 10:15", "--json"]
        if kind == "cfd":
            args += ["--instrument", descriptor]
            chart_path = os.path.join(directory, "chart.json")
            with open(chart_path, "w") as handle:
                handle.write(raw)
            code, output, error = run(scanner, ["orb", "--side", side, "--instrument", descriptor,
                                                "--offline", chart_path, "--now", "2026-10-19 10:15", "--no-write", "--json"])
            assert code == 0, error
            cfd_scan = json.loads(output)
        status, text, error = run(exitp, args)
        assert status == 0, error
        exit_plan = json.loads(text)
    price = checked_entry["trigger"]["price"]
    market_bars = [{"close_time": (dt.datetime.fromtimestamp(bar["t"], dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat(),
                    "close": bar["c"]} for bar in chart["bars"][-3:]]
    market = {"symbol": symbol, "instrument_type": kind, "currency": "USD", "mapping_verified": True,
              "same_price_basis": True, "source": "SYNTHETIC exact instrument bars", "as_of": STAMP,
              "interval": "5m", "bars": market_bars, "sentiment_period": "Letzte 24 Stunden",
              "period_verified": True, "period_change_pct": -2 if side == "sell" else 2}
    if kind == "cfd":
        market.update(contract=instrument["contract"], price_basis="mid")
    scan = {"created_by": "trader-scanner", "created_at": STAMP, "mode": "orb", "as_of": "2026-10-19 10:15 ET", "hits": hits}
    if side == "sell":
        scan["side"] = "sell"
    if cfd_scan is not None:
        scan = cfd_scan
    row = {"instrument": symbol + " synthetic paper study", "symbol": symbol, "instrument_type": kind, "currency": "USD",
           "buy_pct": 10 if side == "sell" else 90, "sell_pct": 90 if side == "sell" else 10,
           "daily_change_pct": -2 if side == "sell" else 2, "buy_price": price + 0.05, "sell_price": price - 0.05,
           "leverage": instrument["leverage"],
           "context": {"market": market, "scanner": scan, "entry": checked_entry, "exit": exit_plan,
                       "news": {"checked": STAMP, "items": [], "notes": []},
                       "news_review": {"symbol": symbol, "reviewed": True, "blocking_catalyst": False,
                                       "reason": "SYNTHETIC reviewed news including mapped underlying, not a current assessment"},
                       "risk_inputs": {"as_of": STAMP, "config": config(), "portfolio": {"cash": 5000, "positions": []},
                                       "state": {"orders": []}, "qty": 2 if kind == "cfd" else 20}}}
    if kind == "cfd":
        row["contract"] = instrument["contract"]
    return {"source": "Plus500 +Insights", "synthetic": True,
            "screenshot": {"file": "SYNTHETIC transcription", "captured_at": STAMP, "locale": "en-US",
                           "sentiment_period": "Letzte 24 Stunden", "category": "highest_sell" if side == "sell" else "highest_buy"},
            "candidates": [row]}


class EntryAndExit(unittest.TestCase):
    def test_default_long_still_triggers_above_range_high(self):
        result = entry.evaluate(entry.parse_chart(json.dumps(bars_document()), "TSLA"), NOW)
        self.assertEqual((result["side"], result["level"], result["trigger"]["price"]), ("buy", 101, 102))

    def test_short_waits_for_closed_bar_below_low(self):
        chart = entry.parse_chart(json.dumps(bars_document("sell")), "TSLA")
        self.assertEqual(entry.evaluate(chart, NOW - dt.timedelta(minutes=1), side="sell")["status"], "waiting")
        result = entry.evaluate(chart, NOW, side="sell")
        self.assertEqual((result["level"], result["trigger"]["price"], result["trigger"]["close_time"]), (99, 98, "10:15"))
        self.assertTrue(result["analysis_only"])
        self.assertIn("closed below", " ".join(entry.describe(result)))

    def test_short_level_and_equality_are_directional(self):
        chart = entry.parse_chart(json.dumps(bars_document("sell")), "TSLA")
        self.assertEqual(entry.evaluate(chart, NOW, setup="level", level=98, side="short")["status"], "waiting")
        self.assertEqual(entry.evaluate(chart, NOW, setup="level", level=99, side="short")["trigger"]["price"], 98)

    def test_short_missing_range_bar_cannot_trigger(self):
        chart = entry.parse_chart(json.dumps(bars_document("sell")), "TSLA")
        del chart["bars"][1]
        self.assertEqual(entry.evaluate(chart, NOW, side="sell")["status"], "no-data")

    def test_short_stop_above_entry_and_target_below(self):
        result = exitp.compute(98, "or_mid", None, "r", exitp.D(2), 101, 99, side="sell")
        self.assertEqual((result["stop"], result["target"], result["risk"], result["reward_risk"]), (100, 94, 2, 2))

    def test_short_stop_rounds_up_and_atr_is_added(self):
        result = exitp.compute(98, "price", exitp.D("100.001"), "r", exitp.D(2), side="sell")
        self.assertEqual(result["stop"], exitp.D("100.01"))
        atr = exitp.compute(98, "atr", exitp.D(2), "r", exitp.D(2), atr=1.1, atr_bars=14, side="sell")
        self.assertEqual((atr["stop"], atr["target"]), (exitp.D("100.20"), exitp.D("93.60")))

    def test_short_range_high_and_percent_stops(self):
        self.assertEqual(exitp.parse_stop("or-high"), ("or_high", None))
        result = exitp.compute(98, "or_high", None, "r", exitp.D(2), 101, 99, side="sell")
        self.assertEqual((result["stop"], result["target"]), (101, 92))
        self.assertEqual(exitp.compute(100, "pct", exitp.D(2), "r", exitp.D(2), side="sell")["stop"], 102)

    def test_invalid_stop_target_and_exhausted_short_target(self):
        for side, stop, target in (("buy", 101, 104), ("buy", 98, 99), ("sell", 99, 96), ("sell", 102, 101), ("sell", 200, 0)):
            with self.subTest(side=side, stop=stop, target=target), self.assertRaises(exitp.PlanError):
                exitp.compute(100, "price", exitp.D(stop), "price", exitp.D(target), side=side)

    def test_cfd_subcent_tick_is_preserved(self):
        result = exitp.compute("1.005", "price", exitp.D("1.0111"), "r", exitp.D(2), side="sell", tick_size="0.001")
        self.assertEqual((result["stop"], result["target"]), (exitp.D("1.012"), exitp.D("0.991")))

    def test_entry_file_preserves_cfd_identity_for_exit(self):
        document = full_document("sell", "cfd")
        row = document["candidates"][0]
        result = row["context"]["entry"]
        saved = entry.entry_doc(result, NOW)
        self.assertEqual(saved["contract"], row["contract"])
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "entry.json")
            with open(path, "w") as handle:
                json.dump(saved, handle)
            code, text, error = run(exitp, ["plan", "--from", path, "--now", "2026-10-19 10:15", "--json"])
        self.assertEqual(code, 0, error)
        plan = json.loads(text)
        self.assertEqual((plan["side"], plan["instrument_type"], plan["symbol"]), ("sell", "cfd", "TSLA-CFD-1"))
        self.assertEqual(plan["contract"], row["contract"])

    def test_short_watchlist_inherits_direction(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = bars_document("sell")
            with open(os.path.join(directory, "TSLA.json"), "w") as handle:
                json.dump(raw, handle)
            path = os.path.join(directory, "watchlist.json")
            code, text, error = run(scanner, ["orb", "--side", "sell", "--offline", os.path.join(directory, "TSLA.json"),
                                              "--now", "2026-10-19 10:15", "--no-write", "--json"])
            self.assertEqual(code, 0, error)
            with open(path, "w") as handle:
                handle.write(text)
            code, text, error = run(entry, ["watchlist", "--watchlist", path, "--offline", directory,
                                            "--now", "2026-10-19 10:15", "--json"])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(text)[0]["result"]["side"], "sell")

    def test_cfd_entry_cli_requires_offline_contract_quotes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "instrument.json")
            with open(path, "w") as handle:
                json.dump(model(kind="cfd"), handle)
            with patch.object(socket.socket, "connect", side_effect=AssertionError("no network")):
                code, _, error = run(entry, ["check", "TSLA-CFD-1", "--instrument", path])
            self.assertEqual(code, 1)
            self.assertIn("--offline", error)

    def test_cfd_scanner_rejects_underlying_price_substitution(self):
        with tempfile.TemporaryDirectory() as directory:
            descriptor, chart = (os.path.join(directory, name) for name in ("instrument.json", "chart.json"))
            with open(descriptor, "w") as handle:
                json.dump(model(kind="cfd"), handle)
            with open(chart, "w") as handle:
                json.dump(bars_document(symbol="TSLA"), handle)
            code, _, error = run(scanner, ["orb", "--instrument", descriptor, "--offline", chart,
                                           "--now", "2026-10-19 10:15", "--no-write", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("exact CFD symbol", error)

    def test_cfd_watchlist_preserves_contract_and_short_direction(self):
        document = full_document("sell", "cfd")
        row = document["candidates"][0]
        with tempfile.TemporaryDirectory() as directory:
            watchlist = os.path.join(directory, "watchlist.json")
            with open(watchlist, "w") as handle:
                json.dump(row["context"]["scanner"], handle)
            with open(os.path.join(directory, row["symbol"] + ".json"), "w") as handle:
                json.dump(bars_document("sell", row["symbol"]), handle)
            code, text, error = run(entry, ["watchlist", "--watchlist", watchlist, "--offline", directory,
                                            "--now", "2026-10-19 10:15", "--json"])
        self.assertEqual(code, 0, error)
        result = json.loads(text)[0]["result"]
        self.assertEqual((result["side"], result["symbol"], result["contract"]), ("sell", row["symbol"], row["contract"]))


class ValuationAndSizing(unittest.TestCase):
    def test_long_and_short_loss_and_reward_are_symmetric(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                m = exposure.metrics(model(side), qty=20)
                self.assertEqual(m["risk_per_unit"], exposure.decimal("2.1", "risk"))
                self.assertEqual(exposure.summary(m)["loss_at_stop_usd"], 42)
                self.assertAlmostEqual(float(m["reward_risk"]), 3.9 / 2.1)

    def test_short_sizing_uses_max_loss_and_reserves_collateral(self):
        sizing = budget.size_trade(model("sell"), config(), today=DAY)
        self.assertEqual((sizing["qty"], sizing["max_loss"], sizing["bound_by"]), (23, 48.3, "max loss"))
        self.assertGreater(sizing["cash_required_usd"], sizing["notional_usd"])
        self.assertTrue(budget.bracket_order(sizing)["analysis_only"])

    def test_cfd_contract_size_spread_and_all_costs(self):
        m = exposure.metrics(model(kind="cfd"), qty=2)
        summary = exposure.summary(m)
        self.assertEqual(summary["costs_usd"], 3.9)
        self.assertEqual(summary["loss_at_stop_usd"], 43.9)
        self.assertEqual(summary["margin_required_usd"], 400)
        self.assertEqual(summary["cash_required_usd"], 443.9)
        self.assertAlmostEqual(float(m["reward_risk"]), 38.05 / 21.95)

    def test_cfd_fractional_contract_sizing_rounds_down(self):
        s = budget.size_trade(model(kind="cfd"), config(), today=DAY)
        self.assertEqual((s["qty"], s["max_loss"]), (2.2, 48.29))
        checked = check([budget.bracket_order(s)])
        self.assertEqual((checked["pass"], checked["results"][0]["max_loss"]), (1, 48.29))

    def test_higher_leverage_does_not_reduce_loss_or_enlarge_notional_limit(self):
        plan = model(kind="cfd")
        more = copy.deepcopy(plan)
        more.update(leverage=10, contract=contract(margin_rate=0.1))
        first, second = (budget.size_trade(p, config(), today=DAY) for p in (plan, more))
        self.assertEqual((first["qty"], first["max_loss"], first["notional_usd"]),
                         (second["qty"], second["max_loss"], second["notional_usd"]))
        self.assertEqual(first["margin_required_usd"], 2 * second["margin_required_usd"])

    def test_leverage_can_be_derived_from_verified_margin(self):
        plan = model(kind="cfd")
        del plan["leverage"]
        self.assertEqual(exposure.summary(exposure.metrics(plan))["leverage"], 5)

    def test_leverage_margin_disagreement_is_rejected(self):
        with self.assertRaises(exposure.ExposureError):
            exposure.metrics(model(kind="cfd", leverage=10))

    def test_optional_stock_long_leverage_changes_margin_not_loss(self):
        first = exposure.summary(exposure.metrics(model(), qty=2))
        second = exposure.summary(exposure.metrics(model(leverage=2), qty=2))
        self.assertEqual(first["loss_at_stop_usd"], second["loss_at_stop_usd"])
        self.assertEqual(first["margin_required_usd"], 2 * second["margin_required_usd"])
        self.assertEqual(check([proposal(model(leverage=2), 2)])["pass"], 1)

    def test_spread_reduces_position_size_and_net_reward(self):
        no_spread = budget.size_trade(model(spread=0), config(), today=DAY)
        spread = budget.size_trade(model(spread=0.5), config(), today=DAY)
        self.assertEqual((no_spread["qty"], spread["qty"]), (25, 20))
        self.assertLess(spread["reward_risk"], no_spread["reward_risk"])

    def test_fresh_lower_short_price_increases_risk(self):
        s = budget.size_trade(model("sell"), config(), price=99, today=DAY)
        self.assertEqual((s["entry"], s["qty"], s["entry_from"]), (99, 16, "price"))
        result = check([proposal(model("sell"), 20)], prices={"TSLA": 99})["results"][0]
        self.assertEqual((result["entry_ref"], result["max_loss"], result["verdict"]), (99, 62, "BLOCK"))
        higher = budget.size_trade(model("sell"), config(), price=101, today=DAY)
        self.assertEqual(higher["entry"], 100)

    def test_guaranteed_and_unguaranteed_maximum_losses_are_distinct(self):
        unbounded = exposure.summary(exposure.metrics(model("sell", "cfd"), qty=2))
        self.assertIsNone(unbounded["maximum_possible_loss_usd"])
        plan = model("sell", "cfd", contract=contract(guaranteed_stop=True, guaranteed_stop_fee_per_contract=0.2))
        guaranteed = exposure.summary(exposure.metrics(plan, qty=2))
        self.assertEqual(guaranteed["maximum_possible_loss_usd"], 44.3)
        self.assertIn("Guaranteed", guaranteed["loss_bound_note"])
        self.assertTrue(any("overnight" in text.lower() for text in unbounded["cost_notes"]))

    def test_stock_short_has_no_finite_maximum_and_long_can_lose_notional(self):
        self.assertIsNone(exposure.summary(exposure.metrics(model("sell")))["maximum_possible_loss_usd"])
        self.assertEqual(exposure.summary(exposure.metrics(model()))["maximum_possible_loss_usd"], 100.1)


class PaperRisk(unittest.TestCase):
    def test_long_short_cfd_long_and_cfd_short_pass_actual_checker(self):
        for kind in ("stock", "cfd"):
            for side in ("buy", "sell"):
                with self.subTest(kind=kind, side=side):
                    result = check([proposal(model(side, kind), 2)])
                    self.assertEqual(result["pass"], 1, result)
                    self.assertTrue(result["analysis_only"])
                    self.assertTrue(result["results"][0]["fingerprint"].startswith("analysis:"))

    def test_loss_cap_exact_boundary_includes_spread(self):
        plan = model(spread=0.5)
        self.assertEqual(check([proposal(plan, 20)])["pass"], 1)
        result = check([proposal(plan, 21)])["results"][0]
        self.assertEqual((result["max_loss"], result["verdict"]), (52.5, "BLOCK"))
        self.assertTrue(any("max_loss_per_trade" in text for text in result["reasons"]))

    def test_margin_and_short_proceeds_never_create_buying_power(self):
        result = check([proposal(model("sell"), 20)])
        self.assertLess(result["cash_after"], 5000)
        poor = check([proposal(model("sell", "cfd"), 2)], portfolio={"cash": 600, "equity": 5000, "positions": []})
        self.assertTrue(any("margin" in reason for reason in poor["results"][0]["reasons"]))

    def test_batch_cash_and_daily_loss_limits_are_enforced(self):
        a = model(kind="cfd")
        b = model(kind="cfd", symbol="SECOND-CFD")
        result = check([proposal(a, 2), proposal(b, 2)], cfg=config(max_daily_loss_usd=70))
        self.assertEqual([r["verdict"] for r in result["results"]], ["PASS", "BLOCK"])
        self.assertIn("daily loss", " ".join(result["results"][1]["reasons"]))
        result = check([proposal(a, 2), proposal(b, 2)], portfolio={"cash": 1200, "equity": 5000, "positions": []})
        self.assertEqual([r["verdict"] for r in result["results"]], ["PASS", "BLOCK"])
        self.assertIn("margin", " ".join(result["results"][1]["reasons"]))

    def test_every_required_cfd_field_blocks_when_missing(self):
        for field in contract():
            with self.subTest(field=field):
                plan = model(kind="cfd")
                del plan["contract"][field]
                result = check([proposal(plan, 1)])
                self.assertEqual(result["block"], 1, result)

    def test_bad_quantities_ticks_and_nonfinite_data_block(self):
        for qty in (0, -1, 0.05, 0.15, True, float("nan")):
            with self.subTest(qty=qty):
                self.assertEqual(check([proposal(model(kind="cfd"), qty)])["block"], 1)
        for field in ("entry", "stop", "target", "spread", "leverage"):
            with self.subTest(field=field):
                plan = model(kind="cfd")
                plan[field] = float("inf")
                self.assertEqual(check([proposal(plan, 1)])["block"], 1)
        self.assertEqual(check([proposal(model(kind="cfd", entry=100.001), 1)])["block"], 1)

    def test_live_mode_and_missing_explicit_permissions_block(self):
        for cfg in (config(paper=False), config(allow_cfd=False), config(allow_margin=False), config(allow_short=False)):
            with self.subTest(cfg=cfg):
                self.assertEqual(check([proposal(model("sell", "cfd"), 1)], cfg=cfg)["block"], 1)

    def test_unknown_account_or_daily_risk_cannot_pass(self):
        for portfolio in ({"cash": 5000}, {"cash": 5000, "positions": [{"symbol": "NVDA"}]}):
            self.assertEqual(check([proposal(model(kind="cfd"), 1)], portfolio=portfolio)["block"], 1)
        state = {"orders": [{"date": DAY, "side": "buy", "kind": "entry", "symbol": "NVDA", "status": "placed"}]}
        self.assertEqual(check([proposal(model(), 1)], state=state)["block"], 1)

    def test_optional_hard_loss_cap_rejects_unguaranteed_cfd(self):
        result = check([proposal(model(kind="cfd"), 1)], cfg=config(require_bounded_loss=True))
        self.assertEqual(result["block"], 1)

    def test_hard_loss_cap_compares_full_long_loss_with_limit(self):
        result = check([proposal(model(), 2)], cfg=config(require_bounded_loss=True))
        self.assertEqual(result["block"], 1)
        self.assertIn("maximum possible loss", " ".join(result["results"][0]["reasons"]))

    def test_disabling_entry_window_does_not_allow_weekend_cfd(self):
        plan = model(kind="cfd", date="2026-10-24")
        checked = risk.check([proposal(plan, 1)], config(no_entries_before=None, no_entries_after=None),
                             {"cash": 5000, "positions": []}, state={"orders": []}, now=dt.datetime(2026, 10, 24, 14, 15))
        self.assertEqual(checked["block"], 1)
        self.assertIn("NYSE session", " ".join(checked["results"][0]["reasons"]))

    def test_analysis_budget_cli_cannot_write_order_file(self):
        with tempfile.TemporaryDirectory() as directory:
            args = ["--now", "2026-10-19 10:15", "--json", "--write", os.path.join(directory, "orders.json")]
            for key, doc in (("plan", model("sell", "cfd")), ("config", config())):
                path = os.path.join(directory, key + ".json")
                with open(path, "w") as handle:
                    json.dump(doc, handle)
                args += ["--" + key, path]
            args += ["--portfolio", os.path.join(directory, "absent.json")]
            code, _, error = run(budget, args)
            self.assertEqual(code, 1)
            self.assertIn("stdout only", error)
            self.assertFalse(os.path.exists(os.path.join(directory, "orders.json")))

    def test_options_futures_and_non_stock_cfds_are_rejected(self):
        for kind in ("option", "future"):
            self.assertEqual(check([proposal(model(kind=kind), 1)])["block"], 1)
        self.assertEqual(check([proposal(model(kind="cfd", contract=contract(underlying_type="future")), 1)])["block"], 1)

    def test_stale_and_future_short_quotes_do_not_understate_loss(self):
        for time in (NOW - dt.timedelta(hours=1), NOW + dt.timedelta(seconds=1)):
            result = check([proposal(model("sell"), 1)], prices={"TSLA": 99}, times={"TSLA": time})
            self.assertEqual(result["block"], 1)

    def test_cfd_cannot_fall_through_legacy_stock_checks(self):
        order = proposal(model(kind="cfd"), 1)
        del order["analysis_only"]
        self.assertEqual(check([order])["block"], 1)

    def test_cli_analysis_writes_no_broker_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            files = {"orders": {"orders": [proposal(model("sell", "cfd"), 1)]}, "config": config(),
                     "portfolio": {"cash": 5000, "positions": []}, "state": {"orders": []}}
            args = ["--now", "2026-10-19 10:15", "--json", "--out", os.path.join(directory, "approval.json")]
            for key, document in files.items():
                path = os.path.join(directory, key + ".json")
                with open(path, "w") as handle:
                    json.dump(document, handle)
                args += ["--" + key, path]
            args += ["--prices", os.path.join(directory, "absent.json")]
            code, text, error = run(risk, args)
            self.assertEqual(code, 0, error)
            self.assertTrue(json.loads(text)["analysis_only"])
            self.assertFalse(os.path.exists(os.path.join(directory, "approval.json")))


class CompleteSentiment(unittest.TestCase):
    def result(self, side="buy", kind="stock", edit=None):
        document = full_document(side, kind)
        if edit:
            edit(document["candidates"][0])
        with patch.object(socket.socket, "connect", side_effect=AssertionError("offline only")):
            return sentiment.analyze(document, STAMP)["candidates"][0]

    def test_normal_long_runs_entry_exit_budget_and_risk(self):
        result = self.result()
        self.assertEqual(result["decision"], "LONG", result)
        self.assertIsNotNone(result["sizing"])

    def test_normal_short_is_no_longer_rejected_for_its_direction(self):
        result = self.result("sell")
        self.assertEqual(result["decision"], "SHORT", result)
        self.assertGreater(result["stop"], 98)
        self.assertLess(result["target"], 98)
        self.assertEqual(result["risk"]["results"][0]["verdict"], "PASS")

    def test_cfd_long_with_complete_contract_evidence(self):
        result = self.result(kind="cfd")
        self.assertEqual(result["decision"], "LONG", result)
        self.assertEqual(result["risk_summary"]["loss_at_stop_usd"], 43.9)
        self.assertIsNone(result["risk_summary"]["maximum_possible_loss_usd"])

    def test_cfd_short_with_complete_contract_evidence(self):
        result = self.result("sell", "cfd")
        self.assertEqual(result["decision"], "SHORT", result)
        self.assertEqual(result["risk_summary"]["margin_required_usd"], 400)

    def test_missing_or_unverified_cfd_data_remains_no_trade(self):
        for field in contract():
            def edit(row):
                # Copies prevent a shared mutable test reference from hiding mismatch checks.
                row["contract"] = copy.deepcopy(row["contract"])
                del row["contract"][field]
            with self.subTest(field=field):
                self.assertEqual(self.result("sell", "cfd", edit)["decision"], "KEIN TRADE")

    def test_cfd_identity_cannot_be_replaced_by_underlying_stock(self):
        result = self.result("sell", "cfd", lambda row: row["context"]["entry"].update(symbol="TSLA"))
        self.assertEqual(result["decision"], "KEIN TRADE")

    def test_short_rejects_long_scanner_or_entry_evidence(self):
        for component in ("scanner", "entry", "exit"):
            result = self.result("sell", edit=lambda row: row["context"][component].update(side="buy"))
            self.assertEqual(result["decision"], "KEIN TRADE", result)

    def test_stop_target_errors_still_block_in_both_directions(self):
        for side, field, value in (("sell", "stop", 97), ("sell", "target", 99), ("buy", "stop", 103), ("buy", "target", 101)):
            with self.subTest(side=side, field=field):
                result = self.result(side, "cfd", lambda row: row["context"]["exit"].update({field: value}))
                self.assertEqual(result["decision"], "KEIN TRADE")

    def test_oversize_and_maximum_loss_limit_still_block(self):
        result = self.result("sell", "cfd", lambda row: row["context"]["risk_inputs"].update(qty=3))
        self.assertEqual(result["decision"], "KEIN TRADE")
        self.assertEqual(result["risk"]["results"][0]["verdict"], "BLOCK")

    def test_report_discloses_margin_loss_bound_and_overnight_costs(self):
        report = sentiment.analyze(full_document("sell", "cfd"), STAMP)
        text = sentiment.render(report)
        for expected in ("SHORT", "Margin", "43.9", "Obergrenze", "overnight", "leverage"):
            self.assertIn(expected, text)

    def test_verified_guaranteed_cfd_stop_passes_strict_loss_cap(self):
        def edit(row):
            terms = contract(guaranteed_stop=True, guaranteed_stop_fee_per_contract=0.2)
            row["contract"] = terms
            for key in ("market", "scanner", "entry", "exit"):
                row["context"][key]["contract"] = copy.deepcopy(terms)
            row["context"]["risk_inputs"]["config"]["require_bounded_loss"] = True
        result = self.result("sell", "cfd", edit)
        self.assertEqual(result["decision"], "SHORT", result)
        self.assertEqual(result["risk_summary"]["maximum_possible_loss_usd"], 44.3)


if __name__ == "__main__":
    unittest.main()
