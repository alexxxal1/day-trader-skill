"""Daily-only adapter equivalence, causal cohorts and reproducible controls."""
import copy
import datetime as dt
import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    import numpy as np
    import pandas
except ImportError as exc:
    raise unittest.SkipTest("Optional daily-history tests need NumPy and Pandas") from exc

from _load import ROOT

sys.path.insert(0, os.path.join(ROOT, "trader-backtest"))
from common import Clock, calendar_2024_2027, iso, module, read, write
from daily_collect import validate_response
from daily_history import HistoricalClock, continuous_runs, mapped, native_decisions, normalize
from daily_results import forward_vector, periods, random_controls, source_statistics


def fixture(n=310, constant=False, split=False):
    sessions = calendar_2024_2027()
    days = [d for d in sorted(sessions) if d <= "2026-11-06"][-n:]
    clock = Clock(sessions)
    timestamps = [(clock.eastern_to_utc(dt.datetime.combine(dt.date.fromisoformat(d), dt.time(9, 30))) - dt.datetime(1970, 1, 1)).total_seconds() for d in days]
    c = [float(17.23 if constant else 50 + .4 * i + .3 * np.sin(i)) for i in range(n)]
    result = {"meta": {"symbol": "TEST", "currency": "USD", "instrumentType": "EQUITY", "dataGranularity": "1d"},
              "timestamp": timestamps,
              "indicators": {"quote": [{"close": c, "open": c, "high": [v + 1 for v in c],
                                          "low": [v - 1 for v in c], "volume": [1000000] * n}]},
              "events": {"splits": {}}}
    if split:
        result["events"]["splits"] = {str(int(timestamps[-20])): {"date": timestamps[-20], "numerator": 3, "denominator": 1}}
    return days, result, clock


class DailyDataIntegrity(unittest.TestCase):
    def test_missing_sessions_do_not_compress_history_or_horizon(self):
        before, after = continuous_runs(np.array([True, True, False, True, True]))
        np.testing.assert_equal(before, [1, 2, 0, 1, 2])
        np.testing.assert_equal(after, [1, 0, 2, 1, 0])
        close = np.array([[100.], [101.], [np.nan], [105.], [110.]])
        self.assertTrue(np.isnan(forward_vector(close, after[:, None], 0, 3, 4)[0]))
        self.assertAlmostEqual(forward_vector(close, after[:, None], 3, 1, 4)[0], 100 * (110 / 105 - 1))

    def test_current_future_split_basis_is_restored(self):
        days, result, _ = fixture(split=True)
        values, valid = normalize(result, days)
        self.assertTrue(valid.all())
        self.assertEqual(values["factor"][0], 3)
        self.assertEqual(values["factor"][-1], 1)

    def test_coarse_yahoo_response_is_rejected(self):
        _, result, _ = fixture()
        result["meta"]["dataGranularity"] = "3mo"
        payload = json.dumps({"chart": {"result": [result]}})
        with self.assertRaisesRegex(ValueError, "WRONG_GRANULARITY"):
            validate_response(payload, "TEST")

    def test_duplicate_day_is_not_arbitrarily_selected(self):
        days, result, _ = fixture()
        result["timestamp"][1] = result["timestamp"][0]
        with self.assertRaisesRegex(ValueError, "DUPLICATE_DAILY_SESSION"):
            normalize(result, days)

    def test_historical_dst_uses_pre_2007_rules(self):
        calendar = {"sessions": {"2006-03-20": {"open_utc": "2006-03-20T14:30:00Z", "close_utc": "2006-03-20T21:00:00Z"}}}
        clock = HistoricalClock(calendar)
        self.assertEqual(clock.session(dt.date(2006, 3, 20)), (dt.time(9, 30), dt.time(16)))
        self.assertEqual(clock.eastern_to_utc(dt.datetime(2006, 3, 20, 9, 30)), dt.datetime(2006, 3, 20, 14, 30))

    def test_eligibility_only_depends_on_past(self):
        valid = np.ones(400, dtype=bool)
        expected = continuous_runs(valid[:300])[0] >= 260
        valid[350] = False
        actual = continuous_runs(valid)[0][:300] >= 260
        np.testing.assert_equal(expected, actual)
        self.assertFalse(actual[258])
        self.assertTrue(actual[259])


class NativeStrategyEquivalence(unittest.TestCase):
    def compare(self, constant=False, split=False):
        days, result, clock = fixture(constant=constant, split=split)
        mini = module("trader-scanner", "minervini")
        q = module("trader-scanner", "qullamaggie")
        values, valid = normalize(result, days)
        eligible = continuous_runs(valid)[0] >= 260
        rank = np.full((4, len(days)), 100.)
        flags, _ = native_decisions(values["c"], values["h"], values["l"], values["v"], eligible, rank, days, factor=values["factor"])
        series = clock.scan.parse_chart({"chart": {"result": [result]}}, "TEST")
        for i in range(259, len(days), 4):
            now = clock.close(days[i])
            original, error = mini.metrics(series, now, clock)
            self.assertIsNone(error)
            expected = mini.evaluate("TEST", original, 100)
            self.assertEqual(bool(flags[i] & 1), expected["trend_template"] == "PASS", days[i])
            bars, error = q.prepare(series, now, clock)
            self.assertIsNone(error)
            status, _, _, _ = q.breakout(bars, dict.fromkeys(("1M", "3M", "6M"), 100), q.config())
            self.assertEqual(bool(flags[i] & 2), status == "CANDIDATE")
            self.assertEqual(bool(flags[i] & 4), status == "SETUP_READY")
            self.assertEqual(bool(flags[i] & 8), bool(flags[i] & 1) and status == "CANDIDATE")
            self.assertEqual(bool(flags[i] & 16), bool(flags[i] & 1) and status == "SETUP_READY")
        return days, values, eligible, rank, flags

    def test_native_functions_match_daily_adapter(self):
        self.compare()

    def test_strict_equalities_and_sequential_mean_boundary(self):
        self.compare(constant=True)

    def test_historical_split_basis_matches_native_normalization(self):
        self.compare(split=True)

    def test_future_observations_do_not_change_past_signals(self):
        days, values, eligible, rank, original = self.compare()
        cutoff = 290
        for key in ("c", "h", "l", "v"):
            values[key][cutoff:] *= 100
        changed, _ = native_decisions(values["c"], values["h"], values["l"], values["v"], eligible, rank, days)
        np.testing.assert_equal(original[:cutoff], changed[:cutoff])

    def test_candidate_is_a_signal_sample_without_any_trade_plan(self):
        _, _, _, _, flags = self.compare()
        self.assertTrue(np.any(flags & 2))
        self.assertTrue(np.all((flags & 4) == 0))


def tiny_matrices(folder, missing=False):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    shape = (25, 3)
    days = ["2026-10-%02d" % (i + 1) for i in range(shape[0])]
    meta = {"shape": shape, "universe": ["A", "B", "C"], "days": days,
            "test_first_index": 0, "test_last_index": 24, "flags": ["SURVIVORSHIP_BIAS_RISK"]}
    write(folder / "matrix-metadata.json", meta)
    price = np.array([[100 + i, 100 - i, 100 + .5 * i] for i in range(25)])
    if missing:
        price[10, 1] = np.nan
    for filename, dtype in (("close.f64", "float64"), ("eligible.u8", "uint8"), ("signals.u8", "uint8"), ("forward-contiguous.u8", "uint8")):
        a = mapped(folder / filename, shape, dtype, "w+")
        if filename == "close.f64":
            a[:] = price
        elif filename == "eligible.u8":
            a[:] = np.isfinite(price)
        elif filename == "signals.u8":
            a[:] = 0
            a[:, 0] = 1
            a[:, 2] = 4 | 16
        else:
            for j in range(3):
                a[:, j] = continuous_runs(np.isfinite(price[:, j]))[1]
        a.flush()
    write(folder / "benchmarks.json", {"SPY": {"close": [100 + .25 * i for i in range(25)], "valid": [True] * 25}})
    return meta


class DailyControlAndSplits(unittest.TestCase):
    def test_is_oos_is_chronological_and_crossing_outcomes_are_purged(self):
        with tempfile.TemporaryDirectory() as tmp:
            analysis, output = Path(tmp) / "analysis", Path(tmp) / "results"
            meta = tiny_matrices(analysis)
            result = source_statistics(analysis, output)
            ranges = periods(meta)
            self.assertEqual(ranges["in_sample"], (0, 16))
            self.assertEqual(ranges["out_of_sample"], (17, 24))
            row = result["periods"]["in_sample"]["strategies"]["MINERVINI"]["5"]
            self.assertEqual(row["n"], 12)
            self.assertEqual(row["right_or_boundary_censored"], 5)
            self.assertIsNone(result["periods"]["full"]["strategies"]["MINERVINI"]["60"]["mean_return_pct"])

    def test_random_control_has_1000_draws_and_reproduces_from_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            analysis, one, two = [Path(tmp) / n for n in ("analysis", "one", "two")]
            tiny_matrices(analysis)
            with patch("urllib.request.urlopen", side_effect=AssertionError("offline")):
                source_statistics(analysis, one)
                two.mkdir()
                write(two / "daily-source-statistics.json", read(one / "daily-source-statistics.json"))
                a = random_controls(analysis, one, 1000, 20261109)
                b = random_controls(analysis, two, 1000, 20261109)
            self.assertEqual(a, b)
            row = a["full"]["MINERVINI"]["1"]
            self.assertEqual(row["valid_mean_estimates"], 1000)
            self.assertEqual(row["matched_selected_observations"], 25)
            self.assertEqual(row["random_observed_outcomes_min"], 24)
            self.assertLess(row["p05_random_mean_return_pct"], row["p95_random_mean_return_pct"])

    def test_checkpoint_resume_and_config_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            analysis, output = Path(tmp) / "analysis", Path(tmp) / "results"
            tiny_matrices(analysis)
            source_statistics(analysis, output)
            a = random_controls(analysis, output, 1000)
            self.assertEqual(a, random_controls(analysis, output, 1000))
            with self.assertRaisesRegex(ValueError, "configuration changed"):
                random_controls(analysis, output, 1000, seed=10)

    def test_missing_future_outcomes_are_not_used_to_filter_random_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            analysis, output = Path(tmp) / "analysis", Path(tmp) / "results"
            tiny_matrices(analysis, missing=True)
            source_statistics(analysis, output)
            result = random_controls(analysis, output, 1000)
            row = result["full"]["MINERVINI"]["5"]
            self.assertEqual(row["matched_selected_observations"], 25)
            self.assertLess(row["random_observed_outcomes_min"], row["random_observed_outcomes_max"])
            self.assertIn("NO_FUTURE_SELECTION", row["flags"])

    def test_checkpoint_commits_rng_and_arrays_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            analysis, output = Path(tmp) / "analysis", Path(tmp) / "results"
            tiny_matrices(analysis)
            source_statistics(analysis, output)
            expected = random_controls(analysis, output, 1000)
            # Interruption after checkpoint rename, before sidecar write.
            (output / "random-state.json").unlink()
            self.assertEqual(expected, random_controls(analysis, output, 1000))


if __name__ == "__main__":
    unittest.main()
