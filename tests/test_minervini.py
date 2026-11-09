"""Synthetic OHLC and mocked network tests; no claims about real candidates."""

import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from _load import load, FIX

scan = load("trader-scanner", "scan")
mini = load("trader-scanner", "minervini")
NOW = scan.parse_now("2026-11-09 09:00")


def stamp(day):
    dt = datetime.datetime.combine(day, datetime.time(9, 30))
    return int((scan.eastern_to_utc(dt) - datetime.datetime(1970, 1, 1)).total_seconds())


def history(slope=0.4, n=300, volume=1000000):
    day = datetime.date(2026, 11, 6)
    days = []
    while len(days) < n:
        if scan.session(day):
            days.append(day)
        day -= datetime.timedelta(days=1)
    days.reverse()
    close = [50 + slope * i for i in range(n)]
    return {"t": [stamp(d) for d in days], "c": close,
            "h": [p + 1 for p in close], "l": [p - 1 for p in close],
            "v": [volume] * n, "meta": {"dataGranularity": "1d"},
            "price_basis": "YAHOO_SPLIT_ADJUSTED", "splits": {}}


def population():
    return {"FAST": history(), "MID": history(0.1), "SLOW": history(0.01)}


def payload(sym, series):
    return {"chart": {"result": [{"meta": dict(series["meta"], symbol=sym),
                                  "timestamp": series["t"],
                                  "indicators": {"quote": [{"close": series["c"], "high": series["h"],
                                                            "low": series["l"], "volume": series["v"]}]},
                                  "events": {"splits": series["splits"]}}], "error": None}}


def cli(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(args)
    return code, out.getvalue(), err.getvalue()


class Criteria(unittest.TestCase):
    def setUp(self):
        # Independent boundary example: price exactly 30% above the annual low.
        self.values = {"price": 130.0, "sma50": 125.0, "sma150": 120.0,
                       "sma200": 110.0, "sma200_previous": 108.0,
                       "low_52w": 100.0, "high_52w": 150.0}

    def failed(self, key, **changes):
        row = mini.evaluate("DEMO", dict(self.values, **changes), 90)
        self.assertEqual(row["trend_template"], "FAIL")
        self.assertIn(key, row["failed_criteria"])

    def test_complete_pass(self):
        row = mini.evaluate("DEMO", self.values, 90)
        self.assertEqual(row["trend_template"], "PASS")
        self.assertEqual(row["criteria_met"], 10)
        self.assertEqual(row["failed_criteria"], [])
        self.assertIsNone(row["signal"])
        self.assertTrue(row["analysis_only"])

    def test_price_below_sma50(self):
        self.failed("price_gt_sma50", price=124)

    def test_sma50_below_sma150(self):
        self.failed("sma50_gt_sma150", sma50=119)

    def test_sma150_below_sma200(self):
        self.failed("sma150_gt_sma200", sma150=109)

    def test_sma200_flat(self):
        self.failed("sma200_rising", sma200_previous=110)

    def test_sma200_declining(self):
        self.failed("sma200_rising", sma200_previous=111)

    def test_less_than_30pct_above_low(self):
        self.failed("above_52w_low_ge_30pct", low_52w=100.001)

    def test_more_than_25pct_below_high(self):
        self.failed("below_52w_high_le_25pct", high_52w=174)

    def test_exactly_25pct_below_high_passes(self):
        self.assertEqual(mini.evaluate("DEMO", dict(self.values, high_52w=130 / 0.75), 90)["trend_template"], "PASS")

    def test_rs_below_70_fails_without_rounding(self):
        row = mini.evaluate("DEMO", self.values, 69.99999)
        self.assertEqual(row["failed_criteria"], ["RS_PROXY_ge_70"])

    def test_rs_exactly_70_passes(self):
        self.assertEqual(mini.evaluate("DEMO", self.values, 70)["trend_template"], "PASS")

    def test_strict_sma_comparisons(self):
        for key, changes in (
            ("price_gt_sma150", {"price": 120}), ("price_gt_sma200", {"price": 110}),
            ("price_gt_sma50", {"price": 125}), ("sma150_gt_sma200", {"sma150": 110}),
            ("sma50_gt_sma150", {"sma50": 120}), ("sma50_gt_sma200", {"sma50": 110}),
        ):
            with self.subTest(key=key):
                self.failed(key, **changes)


class History(unittest.TestCase):
    def test_independent_sma_and_annual_numbers(self):
        values, error = mini.metrics(history(), NOW, scan)
        self.assertIsNone(error)
        self.assertAlmostEqual(values["price"], 169.6)
        self.assertAlmostEqual(values["sma50"], 159.8)
        self.assertAlmostEqual(values["sma150"], 139.8)
        self.assertAlmostEqual(values["sma200"], 129.8)
        self.assertAlmostEqual(values["sma200_previous"], 121.4)
        self.assertAlmostEqual(values["low_52w"], 68.2)
        self.assertAlmostEqual(values["high_52w"], 170.6)
        self.assertAlmostEqual(values["return_12m_pct"], (169.6 / 68.8 - 1) * 100)

    def test_259_bars_insufficient_and_260_sufficient(self):
        self.assertIn("INSUFFICIENT_DATA", mini.metrics(history(n=259), NOW, scan)[1])
        self.assertIsNone(mini.metrics(history(n=260), NOW, scan)[1])

    def test_missing_chart_is_explicit_fail(self):
        rows, counts = mini.scan({}, ["MISSING"], NOW, scan)
        self.assertEqual(rows[0]["trend_template"], "FAIL")
        self.assertIn("INSUFFICIENT_DATA", rows[0]["reason"])
        self.assertEqual(counts["insufficient"], 1)
        for key in ("price", "sma50", "sma150", "sma200", "sma200_trend_pct",
                    "above_52w_low_pct", "below_52w_high_pct", "RS_PROXY"):
            self.assertIsNone(rows[0][key])

    def test_missing_high_low_cannot_be_replaced_by_metadata(self):
        s = history()
        s["h"] = None
        s["meta"].update(fiftyTwoWeekHigh=200, fiftyTwoWeekLow=10)
        self.assertIn("INSUFFICIENT_DATA", mini.metrics(s, NOW, scan)[1])

    def test_invalid_ohlc_fails(self):
        for value in (None, float("nan"), float("inf"), 0, -1):
            s = history()
            s["c"][-5] = value
            with self.subTest(value=value):
                self.assertIn("INSUFFICIENT_DATA", mini.metrics(s, NOW, scan)[1])

    def test_high_below_close_fails(self):
        s = history()
        s["h"][-1] = 1
        self.assertIn("INSUFFICIENT_DATA", mini.metrics(s, NOW, scan)[1])

    def test_missing_latest_session_fails_stale(self):
        s = history()
        for key in ("t", "c", "h", "l", "v"):
            s[key].pop()
        self.assertIn("STALE_DATA", mini.metrics(s, NOW, scan)[1])

    def test_future_bars_and_live_metadata_do_not_change_results(self):
        data = population()
        expected = mini.scan(data, list(data), NOW, scan)
        for s in data.values():
            for key, value in (("t", stamp(datetime.date(2026, 11, 10))), ("c", 99999),
                               ("h", 100000), ("l", 0.01), ("v", 999999999)):
                s[key].append(value)
            s["meta"].update(regularMarketPrice=999999, fiftyTwoWeekLow=0.001,
                             fiftyTwoWeekHigh=999999, regularMarketVolume=999999999)
        self.assertEqual(mini.scan(data, list(data), NOW, scan), expected)

    def test_incomplete_session_excluded_until_close(self):
        s = history()
        day = datetime.date(2026, 11, 9)
        for key, value in (("t", stamp(day)), ("c", 170), ("h", 171), ("l", 169), ("v", 10)):
            s[key].append(value)
        before = mini.metrics(s, scan.parse_now("2026-11-09 15:59"), scan)[0]
        after = mini.metrics(s, scan.parse_now("2026-11-09 16:00"), scan)[0]
        self.assertEqual(before["close_date"], "2026-11-06")
        self.assertEqual(after["close_date"], "2026-11-09")

    def test_intraday_data_rejected(self):
        s = history()
        s["meta"]["dataGranularity"] = "5m"
        self.assertIn("daily OHLC", mini.metrics(s, NOW, scan)[1])

    def test_unknown_price_basis_rejected(self):
        s = history()
        s.pop("price_basis")
        self.assertIn("adjustment basis", mini.metrics(s, NOW, scan)[1])

    def test_sma200_custom_lookback(self):
        values, error = mini.metrics(history(), NOW, scan, trend_days=42)
        self.assertIsNone(error)
        self.assertAlmostEqual(values["sma200_previous"], 113.0)

    def test_long_trend_window_requires_additional_history(self):
        self.assertIn("320", mini.metrics(history(), NOW, scan, trend_days=120)[1])

    def test_conflicting_daily_duplicate_rejected(self):
        s = history()
        for key in ("t", "c", "h", "l", "v"):
            s[key].append(s[key][-1])
        s["h"][-1] += 1
        self.assertIn("conflicting", mini.metrics(s, NOW, scan)[1])

    def test_sparse_history_rejected(self):
        s = history()
        # Keep the current bar, stretch older timestamps over several years.
        end = scan.ts_to_eastern(s["t"][-1]).date()
        s["t"] = [stamp(end - datetime.timedelta(days=7 * (299 - i))) for i in range(300)]
        self.assertIn("sparse", mini.metrics(s, NOW, scan)[1])

    def test_holiday_and_early_close(self):
        self.assertEqual(mini.last_session(scan.parse_now("2026-11-26 17:00"), scan), datetime.date(2026, 11, 25))
        self.assertEqual(mini.last_session(scan.parse_now("2026-11-27 13:00"), scan), datetime.date(2026, 11, 27))


class Splits(unittest.TestCase):
    def test_raw_split_and_adjusted_ohlc_give_same_template_and_ranking(self):
        data = population()
        expected = mini.scan(data, list(data), NOW, scan)
        raw = data["FAST"]
        raw["price_basis"] = "RAW"
        split_index = 200
        raw["splits"] = {"split": {"date": raw["t"][split_index], "numerator": 10, "denominator": 1}}
        for i in range(split_index):
            for key in ("c", "h", "l"):
                raw[key][i] *= 10
            raw["v"][i] /= 10
        actual = mini.scan(data, list(data), NOW, scan)
        for before, after in zip(expected[0], actual[0]):
            self.assertEqual(before["trend_template"], after["trend_template"])
            self.assertEqual(before["RS_PROXY"], after["RS_PROXY"])
            for key in ("sma50", "sma150", "sma200", "low_52w", "high_52w", "return_12m_pct"):
                self.assertAlmostEqual(before[key], after[key])

    def test_yahoo_split_adjusted_prices_are_not_adjusted_twice(self):
        s = history()
        expected = mini.metrics(s, NOW, scan)
        s["splits"] = {"split": {"date": s["t"][200], "numerator": 10, "denominator": 1}}
        self.assertEqual(mini.metrics(s, NOW, scan), expected)

    def test_historical_yahoo_snapshot_undoes_future_split(self):
        s = history()
        expected = mini.metrics(s, NOW, scan)[0]
        for key in ("c", "h", "l"):
            s[key] = [v / 10 for v in s[key]]
        s["v"] = [v * 10 for v in s["v"]]
        s["splits"] = {"later": {"date": stamp(datetime.date(2026, 11, 10)), "numerator": 10, "denominator": 1}}
        actual = mini.metrics(s, NOW, scan)[0]
        for key in ("price", "sma200", "low_52w", "high_52w", "dollar_volume_20d"):
            self.assertAlmostEqual(actual[key], expected[key])

    def test_invalid_split_fails_instead_of_assuming_prices(self):
        s = history()
        s["splits"] = {"bad": {"date": s["t"][100], "numerator": 10, "denominator": 0}}
        self.assertIn("invalid split", mini.metrics(s, NOW, scan)[1])

    def test_parser_preserves_split_basis_and_events(self):
        s = history()
        s["splits"] = {"split": {"date": s["t"][200], "numerator": 2, "denominator": 1}}
        doc = payload("FAST", s)
        # Dividend adjusted adjclose must not silently replace split-adjusted OHLC.
        doc["chart"]["result"][0]["indicators"]["adjclose"] = [{"adjclose": [1] * 300}]
        actual = scan.parse_chart(doc, "FAST")
        self.assertEqual(actual["splits"], s["splits"])
        self.assertEqual(actual["c"], s["c"])
        self.assertEqual(actual["price_basis"], "YAHOO_SPLIT_ADJUSTED")


class Ranking(unittest.TestCase):
    def test_ranking_multiple_stocks(self):
        data = population()
        rows, counts = mini.scan(data, list(data), NOW, scan)
        self.assertEqual([(r["symbol"], r["RS_PROXY"]) for r in rows], [("FAST", 100), ("MID", 50), ("SLOW", 0)])
        self.assertEqual(counts["pass"], 1)

    def test_rank_exactly_70_in_universe(self):
        ranks = mini.percentile_ranks({str(i): i for i in range(11)})
        self.assertEqual(ranks["7"], 70)

    def test_ties_share_midrank(self):
        self.assertEqual(mini.percentile_ranks({"A": 1, "B": 1, "C": 3}), {"A": 25, "B": 25, "C": 100})
        self.assertEqual(mini.percentile_ranks({"A": 1, "B": 1}), {"A": 50, "B": 50})

    def test_single_stock_does_not_get_a_fake_rank(self):
        rows, counts = mini.scan({"FAST": history()}, ["FAST"], NOW, scan)
        self.assertEqual(counts["ranked"], 0)
        self.assertIsNone(rows[0]["RS_PROXY"])
        self.assertEqual(rows[0]["trend_template"], "FAIL")

    def test_stale_and_insufficient_peers_excluded_transparently(self):
        data = population()
        data["SHORT"] = history(n=10)
        data["OLD"] = history()
        for key in ("t", "c", "h", "l", "v"):
            data["OLD"][key].pop()
        rows, counts = mini.scan(data, list(data), NOW, scan)
        self.assertEqual((counts["ranked"], counts["insufficient"], counts["stale"]), (3, 1, 1))
        self.assertTrue(all(r["RS_PROXY"] is None for r in rows if r["symbol"] in ("OLD", "SHORT")))

    def test_liquidity_sort_with_tied_passes(self):
        data = {"A": history(), "B": history(volume=2000000)}
        data.update({"L%d" % i: history(0.01 + i / 1000) for i in range(8)})
        rows, _ = mini.scan(data, list(data), NOW, scan, sort="liquidity")
        self.assertEqual([r["symbol"] for r in rows[:2]], ["B", "A"])
        self.assertEqual(rows[0]["RS_PROXY"], rows[1]["RS_PROXY"])
        self.assertTrue(all(r["trend_template"] == "PASS" for r in rows[:2]))


class Command(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "charts.json")
        with open(self.path, "w") as f:
            json.dump({s: payload(s, v) for s, v in population().items()}, f)
        self.args = ["minervini", "--offline", self.path, "--now", "2026-11-09 09:00", "--no-write"]

    def test_offline_report_and_no_automatic_long(self):
        code, text, _ = cli(self.args + ["--json"])
        doc = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(doc["mode"], "minervini")
        self.assertEqual((doc["universe"], doc["scanned"], doc["pass_count"]), (3, 3, 1))
        self.assertEqual(len(doc["results"]), 3)
        self.assertEqual(doc["hits"][0]["symbol"], "FAST")
        self.assertNotIn("setups", doc["hits"][0])
        self.assertIsNone(doc["signal"])
        self.assertIn("not the original", doc["note"])

    def test_selection_preserves_full_population_and_rank(self):
        code, text, _ = cli(self.args + ["--symbols", "MID", "--json"])
        doc = json.loads(text)
        self.assertEqual(code, 1)
        self.assertEqual(doc["results"][0]["RS_PROXY"], 50)
        self.assertEqual(doc["rs_method"]["population_ranked"], 3)

    def test_limit_only_limits_selected_symbols(self):
        _, text, _ = cli(self.args + ["--limit", "1", "--json"])
        doc = json.loads(text)
        self.assertEqual(doc["selected"], 1)
        self.assertEqual(doc["rs_method"]["population_ranked"], 3)

    def test_liquidity_floor_does_not_change_rs_or_template(self):
        _, text, _ = cli(self.args + ["--min-volume", "99999999", "--json"])
        doc = json.loads(text)
        self.assertEqual(doc["counts"]["pass"], 1)
        self.assertEqual(doc["pass_count"], 0)
        self.assertEqual(doc["results"][0]["RS_PROXY"], 100)

    def test_plain_output_includes_all_requested_fields_and_fail_reasons(self):
        _, text, _ = cli(self.args + ["--all-results"])
        for label in ("SMA50", "SMA150", "SMA200", "200d Trend", "52W low", "52W high", "RS_PROXY",
                      "fulfilled criteria", "failed criteria", "PASS", "FAIL", "No automatic LONG"):
            self.assertIn(label, text)

    def test_default_output_preserves_daily_orb_watchlist(self):
        with patch.object(scan, "write_json") as write:
            code, _, _ = cli([a for a in self.args if a != "--no-write"])
        self.assertEqual(code, 0)
        self.assertEqual(write.call_args[0][0], "trader/minervini.json")

    def test_partial_coverage_report_includes_missing_symbol(self):
        universe = os.path.join(self.tmp.name, "universe.txt")
        with open(universe, "w") as f:
            f.write("FAST\nMID\nSLOW\nMISSING\n")
        _, text, _ = cli(self.args + ["--universe", universe, "--json"])
        doc = json.loads(text)
        self.assertTrue(doc["partial"])
        self.assertEqual(doc["failed"], 1)
        self.assertEqual(len(doc["results"]), 4)
        self.assertEqual(doc["rs_method"]["population_ranked"], 3)

    def test_invalid_options_and_symbol_outside_population(self):
        for args in (["--workers", "0"], ["--pause", "-1"], ["--trend-days", "0"],
                     ["--limit", "-1"], ["--show", "-1"], ["--symbols", "NOPE"], ["--save-data", self.tmp.name],
                     ["--pause", "nan"], ["--min-price", "nan"], ["--min-volume", "-1"]):
            with self.subTest(args=args):
                code, _, error = cli(self.args + args)
                self.assertEqual(code, 1)
                self.assertNotIn("Traceback", error)

    def test_live_downloads_full_population_not_just_selection_and_saves_snapshots(self):
        universe = os.path.join(self.tmp.name, "universe.txt")
        snapshots = os.path.join(self.tmp.name, "snapshots")
        with open(universe, "w") as f:
            f.write("FAST\nMID\nSLOW\n")
        calls = []
        def get(path):
            calls.append(path)
            sym = path.split("/chart/")[1].split("?")[0]
            return json.dumps(payload(sym, population()[sym]))
        with patch.object(scan, "yahoo_get", side_effect=get):
            code, text, _ = cli(["minervini", "--universe", universe, "--symbols", "FAST", "--workers", "2",
                                 "--pause", "0", "--save-data", snapshots, "--now", "2026-11-09 09:00",
                                 "--no-write", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all("range=2y&interval=1d&events=splits" in p for p in calls))
        self.assertEqual(len(os.listdir(snapshots)), 3)
        self.assertEqual(json.loads(text)["rs_method"]["population_ranked"], 3)

    def test_network_failure_is_partial_and_explicit(self):
        universe = os.path.join(self.tmp.name, "universe.txt")
        with open(universe, "w") as f:
            f.write("FAST\nMID\nMISSING\n")
        def get(path):
            sym = path.split("/chart/")[1].split("?")[0]
            if sym == "MISSING":
                raise scan.ScanError("HTTP 429")
            return json.dumps(payload(sym, population()[sym]))
        with patch.object(scan, "yahoo_get", side_effect=get):
            _, text, _ = cli(["minervini", "--universe", universe, "--pause", "0", "--no-write", "--json",
                              "--now", "2026-11-09 09:00"])
        doc = json.loads(text)
        self.assertTrue(doc["partial"])
        missing = next(r for r in doc["results"] if r["symbol"] == "MISSING")
        self.assertEqual(missing["data_error"], "HTTP 429")
        self.assertEqual(missing["trend_template"], "FAIL")

    def test_existing_scanner_modes_unchanged(self):
        root = os.path.join(FIX, "trader-scanner")
        _, text, _ = cli(["daily", "--offline", os.path.join(root, "daily-spark-2026-10-19.json"),
                          "--now", "2026-10-19 09:00", "--no-write", "--json"])
        self.assertEqual(json.loads(text)["mode"], "daily")
        _, text, _ = cli(["orb", "--offline", os.path.join(root, "wall5m-2026-10-19.json.gz"),
                          "--now", "2026-10-19 10:15", "--min-price", "0", "--no-write", "--json"])
        self.assertEqual(len(json.loads(text)["hits"]), 42)
        code, text, _ = cli(["universe", "--offline", os.path.join(root, "symdir"), "--no-write", "--json"])
        self.assertEqual((code, json.loads(text)["count"]), (0, 17))


if __name__ == "__main__":
    unittest.main()
