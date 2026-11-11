"""Event boundaries, objective source checks, overlap and matched controls."""
import copy
import datetime as dt
import os
from pathlib import Path
import sys
import tempfile
import unittest

try:
    import numpy as np
    import pandas as pd
except ImportError as exc:
    raise unittest.SkipTest("Optional event hardening requires NumPy and Pandas") from exc

from _load import ROOT
sys.path.insert(0, os.path.join(ROOT, "trader-backtest"))
from common import read, write
from daily_history import mapped
from data_quality import VALID, SUSPECT, INVALID, classify, window_quality
from event_signals import extract_events, nonoverlap_indices, previous_state_label
from event_statistics import distribution, greedy_nonoverlap, overlap_count, statistics
from event_random import draw_spaced, summarize
from event_random_grouped import draw_group, _group
from test_daily_backtest import tiny_matrices

METHOD = read(Path(ROOT) / "trader-backtest/event-methodology.json")


class StateEvents(unittest.TestCase):
    def test_confluence_ledger_labels_composite_inactivity_not_just_breakout(self):
        self.assertEqual(previous_state_label("CONFLUENCE_SETUP_READY", 4), "NO_SETUP_CONFLUENCE|BREAKOUT=SETUP_READY")
        self.assertEqual(previous_state_label("BREAKOUT_SETUP_READY", 4), "SETUP_READY")

    def test_ongoing_activation_does_not_generate_repeated_events(self):
        flags = np.r_[0, np.full(50, 1 | 4 | 16)].astype("uint8")
        a, b, _, _ = extract_events(flags, np.ones(len(flags), dtype=bool))
        np.testing.assert_equal(np.flatnonzero(a), [1])
        np.testing.assert_equal(np.flatnonzero(b), [1])

    def test_candidate_does_not_fully_reset_a_breakout_episode(self):
        f = np.ones(27, dtype="uint8")
        f[0], f[1], f[2:23], f[23], f[24], f[25:] = 0, 21, 3, 21, 0, 21
        a, b, _, _ = extract_events(f, np.ones(27, dtype=bool))
        np.testing.assert_equal(np.flatnonzero(a & 4), [1, 25])
        np.testing.assert_equal(np.flatnonzero(a & 16), [1, 25])
        np.testing.assert_equal(np.flatnonzero(b & 4), [1, 23])

    def test_minervini_fail_rearms_confluence_even_if_breakout_stays_ready(self):
        f = np.array([0, 21, 4, 21], dtype="uint8")
        a, _, _, _ = extract_events(f, np.ones(4, dtype=bool))
        np.testing.assert_equal(np.flatnonzero(a & 16), [1, 3])
        np.testing.assert_equal(np.flatnonzero(a & 4), [1])

    def test_missing_data_is_not_a_reset_or_an_entry(self):
        f = np.array([0, 21, 0, 21, 3, 21, 0, 21], dtype="uint8")
        eligible = np.array([1, 1, 0, 1, 1, 1, 1, 1], dtype=bool)
        a, _, diagnostics, _ = extract_events(f, eligible)
        np.testing.assert_equal(np.flatnonzero(a & 4), [1, 7])
        self.assertTrue(any(r["left_censored_active_episodes"] for r in diagnostics))

    def test_initial_active_episode_is_left_censored(self):
        a, b, _, _ = extract_events(np.array([21, 21, 0, 21]), np.ones(4, dtype=bool))
        np.testing.assert_equal(np.flatnonzero(a), [3])
        np.testing.assert_equal(np.flatnonzero(b), [3])

    def test_cooldown_requires_new_transition_and_uses_accepted_event_date(self):
        f = np.zeros(50, dtype="uint8")
        f[1:10], f[10], f[11:20], f[20], f[21:] = 1, 0, 1, 0, 1
        _, b, _, _ = extract_events(f, np.ones(50, dtype=bool))
        np.testing.assert_equal(np.flatnonzero(b & 1), [1, 21])
        self.assertFalse(b[41])  # still active, never a periodic re-entry

    def test_future_states_cannot_change_past_events(self):
        f = np.array([0, 21, 3, 21, 0, 21], dtype="uint8")
        a, b, _, _ = extract_events(f, np.ones(6, dtype=bool))
        extended = np.r_[f, np.tile([0, 21], 20)]
        aa, bb, _, _ = extract_events(extended, np.ones(len(extended), dtype=bool))
        np.testing.assert_equal(a, aa[:6])
        np.testing.assert_equal(b, bb[:6])

    def test_quality_filter_never_resurrects_or_rearms_an_event(self):
        f = np.array([0, 21, 21, 21, 0, 21])
        a, _, _, _ = extract_events(f, np.ones(6, dtype=bool))
        original = a.copy()
        quality = np.array([0, 1, 0, 0, 0, 0])
        filtered = a * (quality == VALID)
        self.assertEqual(filtered[1], 0)
        self.assertEqual(filtered[2], 0)
        self.assertEqual(filtered[3], 0)
        np.testing.assert_equal(a, original)


def quote_fixture(closes, volume=None, adj=None):
    dates = pd.bdate_range("2026-01-05", periods=len(closes))
    days = [str(d.date()) for d in dates]
    times = [int(pd.Timestamp(d).tz_localize("America/New_York").replace(hour=9, minute=30).timestamp()) for d in dates]
    result = {"timestamp": times, "meta": {"symbol": "TEST", "currency": "USD", "instrumentType": "EQUITY", "exchangeTimezoneName": "America/New_York"},
              "indicators": {"quote": [{"close": closes, "open": closes, "high": [v * 1.01 if v is not None else None for v in closes],
                  "low": [v * .99 if v is not None else None for v in closes], "volume": volume or [1000] * len(closes)}]}, "events": {}}
    if adj is not None:
        result["indicators"]["adjclose"] = [{"adjclose": adj}]
    return days, result


class ObjectiveQuality(unittest.TestCase):
    def quality(self, closes, volume=None, adj=None):
        days, q = quote_fixture(closes, volume, adj)
        return classify(q, days, METHOD["quality"]), q, days

    def test_null_zero_negative_and_inconsistent_prices_are_invalid(self):
        (s, _, _, _), _, _ = self.quality([100, 0, -1, None, 101])
        np.testing.assert_equal(s[1:4], [INVALID] * 3)
        days, q = quote_fixture([100, 101])
        q["indicators"]["quote"][0]["low"][1] = 102
        self.assertEqual(classify(q, days, METHOD["quality"])[0][1], INVALID)

    def test_symmetric_gross_jump_boundaries_are_suspect_not_proven_invalid(self):
        (s, _, _, _), _, _ = self.quality([100, 200, 100, 110])
        np.testing.assert_equal(s, [VALID, SUSPECT, SUSPECT, VALID])

    def test_correct_split_adjusted_quotes_are_not_rejected_for_split_alone(self):
        days, q = quote_fixture([100, 101, 102], adj=[100, 101, 102])
        q["events"]["splits"] = {"one": {"date": q["timestamp"][1], "numerator": 2, "denominator": 1}}
        s, _, _, actions = classify(q, days, METHOD["quality"])
        np.testing.assert_equal(s, [VALID] * 3)
        self.assertEqual(actions[0]["action_type"], "SPLIT")

    def test_reverse_split_with_confirmed_mixed_adjustment_units_is_invalid(self):
        days, q = quote_fixture([100, 10000, 10000], adj=[10000, 10000, 10000])
        q["events"]["splits"] = {"one": {"date": q["timestamp"][1], "numerator": 1, "denominator": 100}}
        s, reasons, _, actions = classify(q, days, METHOD["quality"])
        self.assertEqual(s[1], INVALID)
        self.assertTrue(reasons[1] & 32)
        self.assertEqual(actions[0]["action_type"], "REVERSE_SPLIT")

    def test_ppcb_shaped_unknown_split_basis_is_flagged_without_symbol_rule(self):
        days, q = quote_fixture([.1, 6250, 2250], adj=[.1, 6250, 2250])
        q["meta"]["symbol"] = "ANY_SYMBOL"
        q["events"]["splits"] = {"one": {"date": q["timestamp"][1], "numerator": 1, "denominator": 2}}
        s, _, _, _ = classify(q, days, METHOD["quality"])
        self.assertEqual(s[1], SUSPECT)
        self.assertEqual(q["indicators"]["quote"][0]["close"][0], .1)

    def test_dividend_explains_adjustment_ratio_change(self):
        days, q = quote_fixture([100, 100, 100], adj=[50, 100, 100])
        self.assertEqual(classify(q, days, METHOD["quality"])[0][1], SUSPECT)
        q["events"]["dividends"] = {"one": {"date": q["timestamp"][1], "amount": 50}}
        self.assertEqual(classify(q, days, METHOD["quality"])[0][1], VALID)

    def test_zero_volume_change_and_stale_series_are_reported(self):
        (s, _, _, _), _, _ = self.quality([100, 101], [1000, 0])
        self.assertEqual(s[1], SUSPECT)
        (s, _, _, _), _, _ = self.quality([100] * 22, [0] * 22)
        self.assertEqual(s[19], VALID)
        self.assertEqual(s[20], SUSPECT)

    def test_declared_inception_mismatch_is_suspect(self):
        days, q = quote_fixture([100] * 6)
        q["meta"]["firstTradeDate"] = q["timestamp"][4]
        s, reasons, _, _ = classify(q, days, METHOD["quality"])
        self.assertEqual(s[0], SUSPECT)
        self.assertTrue(reasons[0] & 128)
        self.assertEqual(s[4], VALID)

    def test_large_multi_day_gain_alone_is_not_deleted(self):
        (s, _, _, _), _, _ = self.quality([100 * 1.5 ** i for i in range(10)])
        np.testing.assert_equal(s, np.zeros(10))

    def test_quality_window_includes_features_and_forward_but_does_not_repair(self):
        bar = np.array([[0], [0], [1], [0], [2], [0]], dtype="uint8")
        feature = np.array([[0], [0], [1], [1], [2], [2]], dtype="uint8")
        self.assertEqual(window_quality(bar, feature, 0, 3, 5)[0], SUSPECT)
        self.assertEqual(window_quality(bar, feature, 0, 4, 5)[0], INVALID)
        self.assertEqual(window_quality(bar, feature, 3, 1, 5)[0], INVALID)


class EventDistributionAndOverlap(unittest.TestCase):
    def test_overlap_and_greedy_removal_are_horizon_specific(self):
        days, assets = np.array([1, 21, 41, 61]), np.zeros(4, dtype=int)
        self.assertEqual(overlap_count(days, assets, 20), 0)
        self.assertEqual(overlap_count(days, assets, 60), 3)
        np.testing.assert_equal(greedy_nonoverlap(days, assets, 60), [True, False, False, True])
        np.testing.assert_equal(nonoverlap_indices(days, 60), [1, 61])

    def test_cross_ticker_same_date_is_not_same_ticker_overlap(self):
        self.assertEqual(overlap_count(np.array([1, 1, 2]), np.array([0, 1, 2]), 60), 0)

    def test_quantiles_thresholds_and_tail_contributions(self):
        r = distribution(np.array([-12, -6, 0, 6, 12]))
        self.assertEqual(r["quantiles"]["25"], -6)
        self.assertEqual(r["quantiles"]["50"], 0)
        self.assertEqual(r["quantiles"]["75"], 6)
        self.assertEqual(r["shares"]["gt_plus5"], .4)
        self.assertEqual(r["shares"]["gt_plus10"], .2)
        self.assertEqual(r["shares"]["lt_minus5"], .4)
        self.assertEqual(r["shares"]["lt_minus10"], .2)
        self.assertAlmostEqual(r["tail"]["top_one_percent_mean_contribution_pp"] + r["tail"]["remaining_mean_contribution_pp"], r["mean"])

    def test_strategy_empirical_percentile_handles_ties(self):
        r = summarize(np.array([1, 2, 2, 3]), 2)
        self.assertEqual(r["strategy_percentile"], 50)
        self.assertEqual(r["fraction_random_better"], .25)


class MatchedEventRandom(unittest.TestCase):
    def test_group_draws_no_duplicates_and_respects_spacing(self):
        rng = np.random.default_rng(20261109)
        last = np.full((1, 100, 4), -100, dtype="int32")
        feasible = np.ones((1, 100), dtype=bool)
        a = draw_group(rng, np.arange(4), last, 1, [2], 2, feasible)[0]
        b = draw_group(rng, np.arange(4), last, 2, [2], 2, feasible)[0]
        self.assertTrue(feasible.all())
        for x, y in zip(a, b):
            self.assertEqual(len(set(x)), 2)
            self.assertFalse(set(x) & set(y))

    def test_quality_matched_layers_obey_same_quality_pool(self):
        rng = np.random.default_rng(1)
        last = np.full((2, 100, 4), -100, dtype="int32")
        allowed = np.array([[1, 1, 1, 1], [0, 0, 1, 1]], dtype=bool)
        feasible = np.ones((2, 100), dtype=bool)
        result = draw_group(rng, np.arange(4), last, 1, [2, 1], 20, feasible, allowed)
        self.assertTrue(np.all(np.isin(result[1, :, 0], [2, 3])))
        self.assertTrue(feasible.all())

    def test_infeasible_draw_does_not_relax_cooldown_or_duplicate(self):
        rng = np.random.default_rng(1)
        last = np.full((1, 10, 2), -100, dtype="int32")
        feasible = np.ones((1, 10), dtype=bool)
        draw_group(rng, np.arange(2), last, 1, [2], 20, feasible)
        draw_group(rng, np.arange(2), last, 2, [1], 20, feasible)
        self.assertFalse(feasible.any())

    def test_uniform_proposals_are_seed_reproducible(self):
        def sample():
            return draw_group(np.random.default_rng(23), np.arange(10), np.full((1, 1000, 10), -100, dtype="int32"),
                              1, [1], 2, np.ones((1, 1000), dtype=bool))
        a, b = sample(), sample()
        np.testing.assert_equal(a, b)
        counts = np.bincount(a.ravel(), minlength=10)
        self.assertTrue(np.all((counts > 50) & (counts < 160)))

    def test_real_group_pipeline_matches_counts_and_atomic_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            analysis, output = Path(tmp) / "analysis", Path(tmp) / "event"
            meta = tiny_matrices(analysis)
            output.mkdir()
            write(output / "event-metadata.json", meta)
            write(output / "frozen-methodology.json", METHOD)
            for variant in ("FULL_RESET", "COOLDOWN20"):
                events = mapped(output / ("events-" + variant + ".u8"), meta["shape"], "uint8", "w+")
                events[:] = 0
                events[[1, 21], 0] = 1
                events[[2, 22], 2] = 4 | 16
                events.flush()
            for name in ("quality-bars.u8", "quality-features.u8"):
                q = mapped(output / name, meta["shape"], "uint8", "w+")
                q[:] = VALID
                q.flush()
            statistics(analysis, output)
            job = (str(analysis), str(output), "COOLDOWN20", "MINERVINI", "VALID_MATCHED")
            a = _group(job)
            b = _group(job)
            self.assertEqual(a, b)
            h = a["horizons"]["1"]
            self.assertEqual(h["feasible_replicates"], 1000)
            row = h["periods"]["full"]["VALID_ONLY"]
            self.assertEqual(row["random_observed_n_min"], row["strategy_n"])
            self.assertEqual(row["random_observed_n_max"], row["strategy_n"])


if __name__ == "__main__":
    unittest.main()
