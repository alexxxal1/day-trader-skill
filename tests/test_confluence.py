"""Confluence contracts, native report integration and point-in-time regressions."""

import contextlib
import copy
import datetime as dt
import io
import json
import os
import tempfile
import unittest

from _load import ROOT, load

c = load("trader-confluence", "confluence")
NOW = "2026-11-09T15:15:00Z"
TRIGGER = "2026-11-09T15:10:00Z"
PRIOR = "2026-11-06"


def trend(status="PASS", **changes):
    row = dict(symbol="DEMO", trend_template=status, close_date=PRIOR, reason=None,
               criteria={"price_gt_sma150": status == "PASS"}, analysis_only=True)
    row.update(changes)
    return dict(created_by="trader-scanner", mode="minervini", created_at=NOW,
                as_of=PRIOR + " close", results=[row])


def setup(variant="BREAKOUT", status="TRIGGERED", **changes):
    short = variant == "PARABOLIC_SHORT"
    row = dict(symbol="DEMO", variant=variant, setup_status=status, daily_as_of=PRIOR,
               trigger={"at": TRIGGER, "price": 100, "level": 101 if short else 99,
                        "type": "ORL_5m" if short else "ORH_5m", "current": True},
               stop={"price": 103 if short else 97, "as_of": TRIGGER}, target=None,
               action="SHORT" if short else "LONG", analysis_only=True, paper=True,
               liquidity_pass=True, risk={"blocks": [], "preferred": True, "warning": None})
    if variant == "EP":
        row["catalyst"] = [{"published_at": "2026-11-09T13:00:00Z", "available_at": "2026-11-09T13:01:00Z",
                            "source_skill": "trader-news", "category": "earnings", "reference": "https://example.test/earnings"}]
    row.update(changes)
    mode = "qullamaggie-parabolic" if variant.startswith("PARABOLIC") else "qullamaggie-ep" if variant == "EP" else "qullamaggie-breakout"
    return dict(created_by="trader-scanner", mode=mode, created_at=NOW, as_of=NOW, results=[row])


def risk(short=False, **changes):
    row = dict(symbol="DEMO", side="sell" if short else "buy", kind="entry", verdict="PASS",
               entry_ref=100, stop=103 if short else 97, target=94 if short else 106,
               fingerprint="synthetic-exact-order", warnings=[], reasons=[])
    row.update(changes)
    return dict(created_by="trader-risk", checked_at=NOW, results=[row], warnings=[])


def event(channel="news", **changes):
    publication_key = {"news": "published_at", "social": "posted_at", "quiver": "disclosure_timestamp", "sentiment": "captured_at"}[channel]
    row = dict(symbol="DEMO", source_skill="trader-" + channel, available_at=NOW,
               reference="https://example.test/" + channel, confirmed=True, direction="LONG")
    row[publication_key] = "2026-11-09T15:00:00Z"
    row.update(changes)
    return row


def analyze(trend_status="PASS", variant="BREAKOUT", status="TRIGGERED", events=None, risk_row=None, **changes):
    reports = [trend(trend_status), setup(variant, status, **changes)]
    reports.append(risk(short=variant == "PARABOLIC_SHORT", **(risk_row or {})))
    return c.analyze({"decision_at": NOW, "reports": reports, "events": events or []})["results"][0]


class Classification(unittest.TestCase):
    def test_breakout_confluence_stages(self):
        for status, label, kind in (("CANDIDATE", "WATCH", "FILTER_CONFLUENCE"),
                                    ("SETUP_READY", "LONG_SETUP", "SETUP_CONFLUENCE"),
                                    ("TRIGGERED", "STRONG_LONG_CONFLUENCE", "TRIGGER_CONFLUENCE")):
            with self.subTest(status=status):
                row = analyze(status=status)
                self.assertEqual(row["confluence"], label)
                self.assertEqual(row["confluence_types"], [kind])

    def test_minervini_fail_does_not_hide_breakout(self):
        for status in c.ACTIVE:
            with self.subTest(status=status):
                row = analyze("FAIL", status=status)
                self.assertEqual(row["minervini"], "FAIL")
                self.assertEqual(row["confluence"], "QULLAMAGGIE_ONLY")

    def test_ep_additional_trend_confluence(self):
        row = analyze(variant="EP")
        self.assertEqual(row["confluence"], "STRONG_LONG_CONFLUENCE")
        self.assertEqual(row["confluence_types"], ["ADDITIONAL_TREND_CONFLUENCE"])

    def test_confirmed_ep_independent_of_failed_or_unknown_trend(self):
        for status in ("FAIL", "INSUFFICIENT_DATA"):
            with self.subTest(status=status):
                row = analyze(status, "EP")
                self.assertEqual(row["confluence"], "EP_ONLY")
                self.assertEqual(row["direction"], "LONG")

    def test_unconfirmed_ep_is_not_ep_only(self):
        row = analyze("FAIL", "EP", catalyst=[])
        self.assertEqual(row["confluence"], "NO_SETUP")
        self.assertEqual(row["qullamaggie_status"]["EP"], "INSUFFICIENT_DATA")

    def test_ep_candidate_is_watch(self):
        self.assertEqual(analyze("FAIL", "EP", "CANDIDATE")["confluence"], "WATCH")

    def test_pass_and_parabolic_short_trigger_is_conflict_even_with_risk_pass(self):
        row = analyze(variant="PARABOLIC_SHORT")
        self.assertEqual(row["confluence"], "CONFLICT")
        self.assertEqual(row["direction"], "NO_TRADE")
        self.assertEqual(row["risk"], "PASS")
        self.assertIn("aktueller", " ".join(row["explanation"]["what_opposes"]))

    def test_short_setup_without_long_trend(self):
        row = analyze("FAIL", "PARABOLIC_SHORT")
        self.assertEqual(row["confluence"], "SHORT_SETUP")
        self.assertEqual(row["direction"], "SHORT")

    def test_untriggered_short_is_visible_without_fabricated_conflict(self):
        self.assertEqual(analyze(variant="PARABOLIC_SHORT", status="SETUP_READY")["confluence"], "SHORT_SETUP")

    def test_parabolic_long_after_failed_trend(self):
        row = analyze("FAIL", "PARABOLIC_LONG")
        self.assertEqual(row["confluence"], "QULLAMAGGIE_ONLY")
        self.assertEqual(row["direction"], "LONG")

    def test_minervini_alone_never_entry(self):
        row = c.analyze({"reports": [trend()], "decision_at": NOW})["results"][0]
        self.assertEqual(row["confluence"], "WATCH")
        self.assertEqual(row["qullamaggie_setup"], ["NONE"])
        self.assertEqual(row["direction"], "NO_TRADE")

    def test_failed_setup_never_recycled_as_trigger(self):
        row = analyze(status="FAILED")
        self.assertEqual(row["confluence"], "WATCH")
        self.assertIsNone(row["trigger"]["BREAKOUT"])

    def test_two_opposite_setups_are_preserved_as_conflict(self):
        doc = {"decision_at": NOW, "reports": [trend("FAIL"), setup(), setup("PARABOLIC_SHORT"), risk(), risk(True)]}
        row = c.analyze(doc)["results"][0]
        self.assertEqual(len(row["setups"]), 2)
        self.assertEqual(row["confluence"], "CONFLICT")
        self.assertEqual(row["direction"], "NO_TRADE")

    def test_two_candidates_do_not_create_ready_or_triggered_setup(self):
        row = c.analyze({"decision_at": NOW, "reports": [trend(), setup(status="CANDIDATE"), setup("EP", "CANDIDATE")]})["results"][0]
        self.assertEqual(row["confluence"], "WATCH")

    def test_latest_failure_supersedes_prior_trigger(self):
        earlier = setup()
        earlier["as_of"] = earlier["created_at"] = "2026-11-09T15:14:00Z"
        row = c.analyze({"decision_at": NOW, "reports": [trend(), earlier, setup(status="FAILED")]})["results"][0]
        self.assertEqual(row["qullamaggie_status"]["BREAKOUT"], "FAILED")

    def test_equal_timestamp_disagreement_is_insufficient(self):
        row = c.analyze({"decision_at": NOW, "reports": [setup(), setup(status="FAILED")]})["results"][0]
        self.assertEqual(row["qullamaggie_status"]["BREAKOUT"], "INSUFFICIENT_DATA")


class Context(unittest.TestCase):
    def test_individual_components(self):
        for channel, options, expected in (
                ("news", {}, "CONFIRMED"), ("news", {"direction": "SHORT"}, "CONFLICTING"),
                ("social", {}, "CONFIRMED"), ("social", {"direction": "SHORT"}, "CONFLICTING"),
                ("sentiment", {}, "CONFIRMING"), ("sentiment", {"assessment": "CONTRARIAN"}, "CONTRARIAN")):
            with self.subTest(channel=channel, options=options):
                self.assertEqual(analyze(events=[event(channel, **options)])[channel], expected)

    def test_old_quiver_is_stale_even_if_observed_now(self):
        row = analyze(events=[event("quiver", disclosure_timestamp="2026-10-01T12:00:00Z")])
        self.assertEqual(row["quiver"], "STALE")
        self.assertEqual(row["confluence"], "STRONG_LONG_CONFLUENCE")

    def test_stale_news_cannot_overrule_price_action(self):
        row = analyze(events=[event(direction="SHORT", published_at="2026-11-01T12:00:00Z")])
        self.assertEqual(row["news"], "UNAVAILABLE")
        self.assertEqual(row["direction"], "LONG")

    def test_disclosure_and_dark_pool_activity_are_context(self):
        for dataset in ("congress", "13f", "darkpool", "dark-pool"):
            with self.subTest(dataset=dataset):
                row = analyze(events=[event("quiver", dataset=dataset)])
                self.assertEqual(row["quiver"], "NEUTRAL")
                self.assertEqual(row["evidence"]["independent_confirmations"], 0)

    def test_quiver_date_only_cannot_confirm_intraday(self):
        row = analyze(events=[event("quiver", disclosure_timestamp="2026-11-09")])
        self.assertEqual(row["quiver"], "UNAVAILABLE")

    def test_missing_optional_feeds_are_explicit(self):
        row = analyze()
        for key in ("news", "social", "quiver", "sentiment"):
            self.assertEqual(row[key], "UNAVAILABLE")

    def test_shared_musk_primary_post_counted_once(self):
        primary = "https://x.com/elonmusk/status/12345"
        row = analyze(events=[event(primary_source=primary), event("social", reference="https://twitter.com/elonmusk/status/12345?utm_source=rss")])
        self.assertEqual(row["news"], "CONFIRMED")
        self.assertEqual(row["social"], "CONFIRMED")
        self.assertEqual(row["evidence"]["independent_confirmations"], 1)
        self.assertEqual(len(row["evidence"]["groups"]), 1)

    def test_event_ids_deduplicate_transitively(self):
        row = analyze(events=[event(event_id="earnings", primary_source="https://example.test/primary"),
                              event("social", event_id="earnings"),
                              event("sentiment", reference="https://example.test/primary#copy")])
        self.assertEqual(len(row["evidence"]["groups"]), 1)

    def test_unrelated_events_remain_separate(self):
        row = analyze(events=[event(), event("social")])
        self.assertEqual(row["evidence"]["independent_confirmations"], 2)

    def test_same_event_opposite_claims_not_independent_confirmation(self):
        row = analyze(events=[event(event_id="shared"), event("social", event_id="shared", direction="SHORT")])
        self.assertEqual(row["evidence"]["independent_confirmations"], 0)

    def test_no_score_or_news_only_entry(self):
        report = c.analyze({"decision_at": NOW, "events": [event(), event("social")]})
        row = report["results"][0]
        self.assertEqual(row["confluence"], "NO_SETUP")
        self.assertEqual(row["direction"], "NO_TRADE")
        self.assertNotIn("score", row)


class Risk(unittest.TestCase):
    def test_fail_blocks_direction_but_preserves_confluence(self):
        row = analyze(risk_row={"verdict": "BLOCK", "reasons": ["daily loss limit"]})
        self.assertEqual(row["risk"], "FAIL")
        self.assertEqual(row["direction"], "NO_TRADE")
        self.assertEqual(row["confluence"], "STRONG_LONG_CONFLUENCE")

    def test_risk_requires_exact_side_and_levels(self):
        for changes in ({"side": "sell"}, {"entry_ref": 101}, {"stop": 96}, {"target": 95},
                        {"kind": "exit"}, {"fingerprint": ""}, {"warnings": ["stale quote"]},
                        {"instrument_type": "cfd"}, {"contract": "other-contract"}):
            with self.subTest(changes=changes):
                self.assertEqual(analyze(risk_row=changes)["direction"], "NO_TRADE")

    def test_scanner_stop_context_is_not_full_risk_pass(self):
        row = c.analyze({"decision_at": NOW, "reports": [trend(), setup()]})["results"][0]
        self.assertEqual(row["risk"], "FAIL")
        self.assertEqual(row["direction"], "NO_TRADE")

    def test_existing_scanner_blocks_and_volatility_limits_preserved(self):
        for context in ({"blocks": ["short disabled"]}, {"warning": True}, {"preferred": False}):
            with self.subTest(context=context):
                self.assertEqual(analyze(risk=context)["risk"], "FAIL")

    def test_existing_scanner_watch_is_not_overridden_by_a_risk_record(self):
        row = analyze(action="WATCH")
        self.assertEqual(row["risk"], "FAIL")
        self.assertEqual(row["direction"], "NO_TRADE")

    def test_target_is_supplied_by_existing_risk_never_invented(self):
        self.assertEqual(analyze()["target"]["BREAKOUT"], 106)
        self.assertEqual(analyze(risk_row={"target": None})["direction"], "NO_TRADE")

    def test_analysis_never_grants_order_permission(self):
        report = c.analyze({"decision_at": NOW, "reports": [trend(), setup(), risk()]})
        self.assertFalse(report["trade_allowed"])
        self.assertIsNone(report["signal"])
        self.assertTrue(report["analysis_only"])
        self.assertFalse(report["results"][0]["trade_allowed"])


class PointInTime(unittest.TestCase):
    def test_no_future_publication_or_observation(self):
        for channel in ("news", "social", "quiver", "sentiment"):
            key = {"news": "published_at", "social": "posted_at", "quiver": "disclosure_timestamp", "sentiment": "captured_at"}[channel]
            for field in (key, "available_at"):
                with self.subTest(channel=channel, field=field):
                    row = analyze(events=[event(channel, **{field: "2026-11-09T15:15:01Z"})])
                    self.assertEqual(row[channel], "UNAVAILABLE")

    def test_exact_decision_boundary_is_available(self):
        self.assertEqual(analyze(events=[event(published_at=NOW)])["news"], "CONFIRMED")

    def test_ambiguous_or_missing_timezone_unavailable(self):
        for value in (None, "2026-11-09 15:00:00", "2026-11-09", "bad"):
            with self.subTest(value=value):
                self.assertEqual(analyze(events=[event(published_at=value)])["news"], "UNAVAILABLE")

    def test_future_snapshot_cannot_be_replayed_as_past(self):
        doc = setup()
        doc["created_at"] = "2026-11-09T15:16:00Z"
        row = c.analyze({"decision_at": NOW, "reports": [trend(), doc]})["results"][0]
        self.assertEqual(row["qullamaggie_status"]["BREAKOUT"], "INSUFFICIENT_DATA")

    def test_later_creation_time_does_not_rejuvenate_stale_setup_state(self):
        doc = setup()
        doc["as_of"] = "2026-11-09T14:50:00Z"
        row = c.analyze({"decision_at": NOW, "reports": [doc]})["results"][0]
        self.assertEqual(row["qullamaggie_status"]["BREAKOUT"], "INSUFFICIENT_DATA")

    def test_future_intraday_trigger_rejected(self):
        row = analyze(trigger={"at": "2026-11-09T15:16:00Z", "price": 100, "type": "ORH_5m", "current": True})
        self.assertEqual(row["qullamaggie_status"]["BREAKOUT"], "INSUFFICIENT_DATA")

    def test_range_must_finish_before_completed_trigger(self):
        for at, kind in (("2026-11-09T14:35:00Z", "ORH_5m"), ("2026-11-09T15:30:00Z", "ORH_60m"),
                         ("2026-11-09T14:34:00Z", "FIRST_GREEN_5m"), ("2026-11-09T14:36:30Z", "VWAP_FAILURE")):
            with self.subTest(at=at, kind=kind):
                row = analyze(trigger={"at": at, "price": 100, "type": kind, "current": True}, stop={"price": 97, "as_of": at})
                self.assertEqual(row["qullamaggie_status"]["BREAKOUT"], "INSUFFICIENT_DATA")

    def test_closed_trigger_bar_after_range_is_valid(self):
        at = "2026-11-09T14:36:00Z"
        self.assertEqual(analyze(trigger={"at": at, "type": "ORH_5m", "price": 100, "current": True},
                                 stop={"price": 97, "as_of": at})["qullamaggie_status"]["BREAKOUT"], "TRIGGERED")

    def test_trigger_direction_must_match_variant(self):
        row = analyze("FAIL", "PARABOLIC_SHORT", trigger={"at": TRIGGER, "price": 100, "type": "ORH_5m", "current": True})
        self.assertEqual(row["qullamaggie_status"]["PARABOLIC_SHORT"], "INSUFFICIENT_DATA")

    def test_risk_must_be_evaluated_after_trigger(self):
        doc = risk()
        doc["checked_at"] = "2026-11-09T15:09:00Z"
        row = c.analyze({"decision_at": NOW, "reports": [trend(), setup(), doc]})["results"][0]
        self.assertEqual(row["risk"], "FAIL")

    def test_equivalent_explicit_timezone_gives_identical_results(self):
        doc = {"decision_at": NOW, "reports": [trend(), setup(), risk()]}
        self.assertEqual(c.analyze(doc), c.analyze(doc, "2026-11-09T16:15:00+01:00"))

    def test_new_year_calendar_boundary_is_bounded(self):
        self.assertIsNone(c.last_completed(c.ev.timestamp("2026-01-01T00:00:00Z")))

    def test_early_close_prevents_late_current_trigger(self):
        doc = setup()
        doc["created_at"] = doc["as_of"] = "2026-11-27T18:05:00Z"
        doc["results"][0].update(daily_as_of="2026-11-25", trigger={"at": "2026-11-27T18:01:00Z", "price": 100,
                                                                           "type": "ORH_5m", "current": True},
                                  stop={"price": 97, "as_of": "2026-11-27T18:01:00Z"})
        row = c.analyze({"decision_at": doc["as_of"], "reports": [doc]})["results"][0]
        self.assertEqual(row["qullamaggie_status"]["BREAKOUT"], "INSUFFICIENT_DATA")

    def test_daily_closing_data_cannot_support_intraday_decision(self):
        for report in (trend(close_date="2026-11-09"), setup(daily_as_of="2026-11-09")):
            row = c.analyze({"decision_at": NOW, "reports": [report]})["results"][0]
            if report["mode"] == "minervini":
                self.assertEqual(row["minervini"], "INSUFFICIENT_DATA")
            else:
                self.assertEqual(row["qullamaggie_status"]["BREAKOUT"], "INSUFFICIENT_DATA")

    def test_minervini_native_fail_for_missing_rs_maps_to_insufficient(self):
        report = trend("FAIL", reason="INSUFFICIENT_DATA: RS_PROXY population", criteria={"RS_PROXY_ge_70": None})
        row = c.analyze({"decision_at": NOW, "reports": [report]})["results"][0]
        self.assertEqual(row["minervini"], "INSUFFICIENT_DATA")

    def test_stale_daily_trend_cannot_confirm_current_setup(self):
        row = c.analyze({"decision_at": NOW, "reports": [trend(close_date="2026-11-05"), setup()]})["results"][0]
        self.assertEqual(row["confluence"], "QULLAMAGGIE_ONLY")

    def test_ep_catalyst_must_be_available_at_trigger(self):
        row = analyze("FAIL", "EP", catalyst=[{"published_at": "2026-11-09T15:11:00Z", "available_at": NOW,
                                               "reference": "https://example.test/late"}])
        self.assertEqual(row["confluence"], "NO_SETUP")

    def test_stop_cannot_be_selected_with_later_information(self):
        row = analyze(stop={"price": 97, "as_of": NOW})
        self.assertEqual(row["qullamaggie_status"]["BREAKOUT"], "INSUFFICIENT_DATA")

    def test_future_and_stale_risk_are_fail(self):
        for stamp in ("2026-11-09T15:16:00Z", "2026-11-09T14:59:00Z"):
            doc = risk()
            doc["checked_at"] = stamp
            row = c.analyze({"decision_at": NOW, "reports": [trend(), setup(), doc]})["results"][0]
            self.assertEqual(row["risk"], "FAIL")

    def test_premarket_does_not_fabricate_current_intraday_trigger(self):
        q = setup()
        q["created_at"] = q["as_of"] = "2026-11-09T07:00:00Z"
        row = c.analyze({"decision_at": q["as_of"], "reports": [q]})["results"][0]
        self.assertEqual(row["qullamaggie_status"]["BREAKOUT"], "INSUFFICIENT_DATA")

    def test_after_close_snapshot_cannot_claim_current_trigger(self):
        doc = setup()
        doc["as_of"] = doc["created_at"] = "2026-11-09T21:01:00Z"
        row = c.analyze({"decision_at": doc["as_of"], "reports": [doc]})["results"][0]
        self.assertNotEqual(row["qullamaggie_status"]["BREAKOUT"], "TRIGGERED")


class NativeReportsAndCLI(unittest.TestCase):
    def test_native_news_stays_neutral_without_causal_review(self):
        doc = {"checked": NOW, "items": [{"ticker": "DEMO", "time": TRIGGER,
                                           "link": "https://example.test/news", "title": "earnings"}]}
        row = c.analyze({"decision_at": NOW, "reports": [doc, setup()]})["results"][0]
        self.assertEqual(row["news"], "NEUTRAL")

    def test_native_news_covers_all_explicitly_affected_tickers(self):
        doc = {"checked": NOW, "items": [{"ticker": "DEMO", "touches": ["DEMO", "OTHER"], "time": TRIGGER,
                                           "link": "https://example.test/news"}]}
        rows = c.analyze({"decision_at": NOW, "reports": [doc]})["results"]
        self.assertEqual([r["ticker"] for r in rows], ["DEMO", "OTHER"])
        self.assertTrue(all(r["news"] == "NEUTRAL" for r in rows))

    def test_native_sentiment_extract_only_cannot_confirm(self):
        doc = {"created_by": "trader-sentiment", "as_of": NOW, "extract_only": True,
               "screenshot": {"captured_at": TRIGGER, "file": "synthetic.png"},
               "candidates": [{"symbol": "DEMO", "decision": "LONG", "bias": "LONG"}]}
        row = c.analyze({"decision_at": NOW, "reports": [doc, setup()]})["results"][0]
        self.assertEqual(row["sentiment"], "NEUTRAL")

    def test_native_social_verified_direction(self):
        doc = {"created_by": "trader-social", "checked_at": NOW, "results": [
            {"ticker": "DEMO", "original_timestamp": TRIGGER, "source_reference": "https://x.com/i/status/123",
             "verification": "VERIFIED", "price_confirmation": "JA", "impact": "BULLISH"}]}
        row = c.analyze({"decision_at": NOW, "reports": [doc, setup()]})["results"][0]
        self.assertEqual(row["social"], "CONFIRMED")

    def test_native_quiver_transaction_date_never_used_for_availability(self):
        doc = {"created_by": "trader-quiver", "checked_at": NOW, "records": [
            {"ticker": "DEMO", "transaction_timestamp": "2026-10-01T10:00:00Z", "disclosure_timestamp": "2026-11-10T10:00:00Z",
             "source_reference": "https://example.test/disclosure", "quality": "COMPLETE"}]}
        row = c.analyze({"decision_at": NOW, "reports": [doc, setup()]})["results"][0]
        self.assertEqual(row["quiver"], "UNAVAILABLE")

    def test_native_sentiment_confirmation_and_contrarian(self):
        for quality, expected in (("hoch (Regeln erfuellt)", "CONFIRMING"), ("mittel (kontraer bestaetigt)", "CONTRARIAN")):
            doc = {"created_by": "trader-sentiment", "as_of": NOW, "screenshot": {"captured_at": TRIGGER, "file": "synthetic.png"},
                   "candidates": [{"extracted": {"symbol": "DEMO"}, "decision": "LONG", "bias": "LONG", "quality": quality}]}
            row = c.analyze({"decision_at": NOW, "reports": [doc, setup()]})["results"][0]
            self.assertEqual(row["sentiment"], expected)

    def test_genuine_native_qullamaggie_output_is_accepted(self):
        from test_qullamaggie import base, intrabars, report
        rows = report("qullamaggie-breakout", bars=base(), intra=intrabars(), now=dt.datetime(2026, 11, 9, 14, 50))
        native = {"created_by": "trader-scanner", "mode": "qullamaggie-breakout", "as_of": "2026-11-09T14:50:00Z", "results": rows}
        result = c.analyze({"decision_at": native["as_of"], "reports": [native]})
        demo = next(r for r in result["results"] if r["ticker"] == "DEMO")
        self.assertEqual(demo["qullamaggie_status"]["BREAKOUT"], "TRIGGERED")

    def test_missing_data_and_requested_tickers(self):
        row = c.analyze({"decision_at": NOW, "tickers": ["MISSING"]})["results"][0]
        self.assertEqual(row["minervini"], "INSUFFICIENT_DATA")
        self.assertEqual(row["confluence"], "NO_SETUP")
        self.assertEqual(row["direction"], "NO_TRADE")

    def test_report_and_input_are_not_mutated(self):
        doc = {"decision_at": NOW, "reports": [trend(), setup(), risk()], "events": [event()]}
        before = copy.deepcopy(doc)
        c.analyze(doc)
        self.assertEqual(doc, before)

    def test_table_and_explanations(self):
        text = c.render(c.analyze({"decision_at": NOW, "reports": [trend(), setup()]}))
        for value in ("Ticker | Minervini", "Qullamaggie Status", "Target", "Begründung", "WARUM interessant?",
                      "WAS bestätigt", "WAS spricht dagegen?", "WAS muss vor einem Trade"):
            self.assertIn(value, text)

    def test_bad_clocks_and_nonfinite_data_fail(self):
        for doc in ({"decision_at": "2026-11-09 15:00"}, {"decision_at": "2025-11-09T15:00:00Z"}, {"x": float("nan")}):
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                c.analyze(doc)

    def test_cli_reads_native_reports_and_writes_analysis_only(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for name, doc in (("minervini", trend()), ("breakout", setup())):
                path = os.path.join(folder, name + ".json")
                with open(path, "w") as stream:
                    json.dump(doc, stream)
                paths.append(path)
            target = os.path.join(folder, "confluence.json")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = c.main(["--minervini", paths[0], "--qullamaggie", paths[1], "--now", NOW, "--out", target, "--json"])
            self.assertEqual(code, 0)
            report = json.loads(output.getvalue())
            self.assertFalse(report["trade_allowed"])
            with open(target) as stream:
                self.assertEqual(json.load(stream), report)
            self.assertEqual(sorted(os.listdir(folder)), ["breakout.json", "confluence.json", "minervini.json"])

    def test_cli_refuses_input_overwrite_and_pipeline_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "input.json")
            with open(source, "w") as stream:
                json.dump({"decision_at": NOW}, stream)
            for path in (source, os.path.join(folder, "orders.json"), os.path.join(folder, "plan.json")):
                with self.subTest(path=path), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(c.main([source, "--out", path]), 2)

    def test_synthetic_example_runs(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = c.main([os.path.join(ROOT, "trader-confluence", "input.example.json"), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["results"][0]["evidence"]["independent_confirmations"], 1)


if __name__ == "__main__":
    unittest.main()
