"""Synthetic offline scenarios only. No fixture describes an actual public post."""

import ast
import copy
import datetime
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

from _load import ROOT, load

social = load("trader-social", "social")
x_api = load("trader-social", "x_api")
NOW = "2026-11-06T15:00:00+00:00"
POSTED = "2026-11-06T14:58:00+00:00"


def synthetic_post(impact="BULLISH", platform="X", symbol="TSLA"):
    """Author labels test account routing, never an invented attributed quote."""
    truth = platform == "Truth Social"
    return {
        "id": "100", "author": "Donald Trump" if truth else "Elon Musk",
        "account": "realDonaldTrump" if truth else "elonmusk", "author_id": "42",
        "platform": platform, "timestamp": POSTED,
        "text": "SYNTHETIC OFFLINE TEST ONLY: $" + symbol,
        "content_kind": "original", "catalyst_type": "Produkt",
        "affected_assets": [{"symbol": symbol, "kind": "Unternehmen", "impact": impact,
                             "basis": "Synthetic analyst assessment for offline testing only"}],
        "source": {"kind": "user_screenshot" if truth else "x_api",
                   "reference": "local-synthetic-attachment.png" if truth else "https://api.x.com/2/users/42/tweets",
                   "reviewed": True, "account_confirmed": True,
                   "provided_by_user": truth, "authenticity_checked": truth,
                   "original_time_confirmed": True}}


def synthetic_evidence(side="LONG", symbol="TSLA"):
    price, stop, target = (102, 100, 106) if side == "LONG" else (98, 100, 94)
    statuses = ("CLEAR", "CONFIRMED", "TRIGGERED", "READY", "PASS", "ALIGNED")
    reports = {}
    for skill, status in zip(social.SKILLS, statuses):
        reports[skill] = {"status": status, "checked_at": NOW, "reviewed": True,
                          "reference": "synthetic-offline-only/" + skill + "/" + symbol,
                          "side": side, "setup_id": "synthetic-setup-" + symbol,
                          "short_capable": side == "SHORT"}
    reports["trader-news"]["conflict"] = False
    reports["trader-entry"].update(entry=price, within_entry_window=True)
    for skill in ("trader-exit", "trader-risk"):
        reports[skill].update(entry=price, stop=stop, target=target)
    return {"tickers": {symbol: {
        "market": {"source": "SYNTHETIC OFFLINE DATA", "reference": "synthetic-equal-duration-windows",
                   "before_at": "2026-11-06T14:57:00+00:00", "price_before": 100,
                   "reaction_at": "2026-11-06T14:59:00+00:00", "price_after": price,
                   "observed_at": NOW, "volume": 3000, "baseline_volume": 1000,
                   "volume_window_seconds": 60, "baseline_window_seconds": 60,
                   "bid": price - 0.1, "ask": price + 0.1,
                   "session": "REGULAR", "trading_open": True, "halted": False},
        "skills": reports}}}


class Offline(unittest.TestCase):
    def setUp(self):
        self.guard = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Unexpected network request"))
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.cfg = social.config()
        self.post = synthetic_post()
        self.evidence = synthetic_evidence()

    def row(self, post=None, evidence=None, now=NOW):
        result = social.analyze([self.post if post is None else post],
                                self.evidence if evidence is None else evidence,
                                self.cfg, social.timestamp(now))
        return result["results"][0]

    def no_trade(self, post=None, evidence=None, contains=None, now=NOW):
        row = self.row(post, evidence, now)
        self.assertEqual(row["decision"], "KEIN TRADE")
        if contains:
            self.assertIn(contains, "; ".join(row["reasons"]))
        return row


class Decisions(Offline):
    def test_verified_bullish_x_post_and_all_confirmations(self):
        row = self.row()
        self.assertEqual(row["decision"], "LONG")
        self.assertEqual(row["verification"], "VERIFIED")
        self.assertEqual(row["directness"], "DIREKT")
        self.assertEqual(row["price_confirmation"], "JA")
        self.assertEqual(row["age_seconds"], 120)
        self.assertEqual(row["market_metrics"]["price_change_pct"], 2)
        self.assertEqual(row["mode"], "paper-analysis")

    def test_verified_bearish_x_with_market_confirmation_requires_explicit_social_opt_in(self):
        row = self.no_trade(synthetic_post("BEARISH"), synthetic_evidence("SHORT"), "SHORT-Analyse deaktiviert")
        self.assertEqual(row["price_confirmation"], "JA")
        self.assertEqual(row["verification"], "VERIFIED")

    def test_verified_bearish_x_with_independent_short_setup(self):
        self.cfg["allow_short_analysis"] = True
        row = self.row(synthetic_post("BEARISH"), synthetic_evidence("SHORT"))
        self.assertEqual(row["decision"], "SHORT")

    def test_native_short_evidence_needs_no_artificial_capability_flag(self):
        self.cfg["allow_short_analysis"] = True
        evidence = synthetic_evidence("SHORT")
        del evidence["tickers"]["TSLA"]["skills"]["trader-entry"]["short_capable"]
        self.assertEqual(self.row(synthetic_post("BEARISH"), evidence)["decision"], "SHORT")

    def test_post_without_market_confirmation(self):
        self.evidence["tickers"]["TSLA"]["market"]["price_after"] = 100.1
        self.no_trade(contains="Keine gerichtete Kursbestätigung")

    def test_post_without_market_data(self):
        del self.evidence["tickers"]["TSLA"]["market"]
        self.no_trade(contains="Marktdaten")

    def test_old_post_is_not_a_current_catalyst(self):
        self.post["timestamp"] = "2026-11-06T13:00:00+00:00"
        self.no_trade(contains="Post zu alt")

    def test_unknown_naive_and_future_post_times(self):
        for time in (None, "unknown", "2026-11-06T14:58:00", "2026-11-06T16:00:00+00:00"):
            with self.subTest(time=time):
                self.post["timestamp"] = time
                self.no_trade(contains="Post-Zeit unbekannt, zukünftig")

    def test_unverified_screenshot(self):
        self.post["source"].update(kind="user_screenshot", provided_by_user=True,
                                   authenticity_checked=False, reference="synthetic-upload.png")
        row = self.no_trade(contains="Social-Daten unverifiziert")
        self.assertEqual(row["verification"], "UNVERIFIED")

    def test_user_provided_truth_social_screenshot(self):
        row = self.row(synthetic_post(platform="Truth Social"))
        self.assertEqual(row["verification"], "VERIFIED")
        self.assertEqual(row["directness"], "DIREKT")
        self.assertIn("keine unabhängige Plattformbestätigung", row["verification_basis"])
        self.assertEqual(row["decision"], "LONG")

    def test_user_provided_truth_social_text(self):
        self.post = synthetic_post(platform="Truth Social")
        self.post["source"].update(kind="user_text", reference="synthetic-user-input")
        self.assertEqual(self.row()["verification"], "VERIFIED")

    def test_unreviewed_user_truth_social_screenshot_is_not_verified(self):
        self.post = synthetic_post(platform="Truth Social")
        self.post["source"]["reviewed"] = False
        self.assertEqual(self.no_trade()["verification"], "UNVERIFIED")

    def test_indirect_truth_social_report_stays_indirect(self):
        self.post = synthetic_post(platform="Truth Social")
        self.post["content_kind"] = "secondary"
        self.post["source"].update(kind="news", reference="https://feeds.finance.yahoo.com/rss/2.0/headline?s=TSLA",
                                   reported_at="2026-11-06T14:59:00+00:00", provided_by_user=False)
        row = self.row()
        self.assertEqual(row["verification"], "CORROBORATED")
        self.assertEqual(row["directness"], "INDIREKT")
        self.assertNotEqual(row["verification"], "VERIFIED")

    def test_indirect_truth_social_without_original_time_has_no_trade(self):
        self.post = synthetic_post(platform="Truth Social")
        self.post["source"].update(kind="news", reference="https://feeds.finance.yahoo.com/rss",
                                   original_time_confirmed=False)
        self.no_trade(contains="Originalzeit des Posts nicht bestätigt")

    def test_new_report_does_not_refresh_old_post(self):
        self.post = synthetic_post(platform="Truth Social")
        self.post["timestamp"] = "2026-11-05T15:00:00+00:00"
        self.post["source"].update(kind="news", reference="https://feeds.finance.yahoo.com/rss", reported_at=NOW)
        self.no_trade(contains="Post zu alt")

    def test_official_truth_social_confirmation_requires_explicit_confirmation(self):
        self.post = synthetic_post(platform="Truth Social")
        self.post["source"].update(kind="official", reference="https://www.sec.gov/synthetic-only", confirms_post=True)
        self.assertEqual(self.row()["verification"], "VERIFIED")
        self.post["source"]["confirms_post"] = False
        self.assertEqual(self.no_trade()["verification"], "UNVERIFIED")

    def test_disallowed_secondary_sources_and_host_spoofing(self):
        for url in ("https://truthsocial.com/post/1", "https://api.truthsocial.com/post/1",
                    "https://feeds.finance.yahoo.com.evil.invalid/rss", "http://feeds.finance.yahoo.com/rss",
                    "https://feeds.finance.yahoo.com@evil.invalid/rss"):
            with self.subTest(url=url):
                self.post["source"].update(kind="news", reference=url)
                self.assertEqual(self.no_trade()["verification"], "UNVERIFIED")

    def test_conflicting_news_blocks_trade(self):
        self.evidence["tickers"]["TSLA"]["skills"]["trader-news"]["conflict"] = True
        self.no_trade(contains="widersprüchliche Evidenz")

    def test_conflicting_social_blocks_trade(self):
        self.post["conflicting"] = True
        self.no_trade(contains="Widersprüchliche Social-Daten")

    def test_opposite_current_posts_block_all_rows_for_the_ticker(self):
        opposite = synthetic_post("BEARISH")
        rows = social.analyze([self.post, opposite], self.evidence, self.cfg, social.timestamp(NOW))["results"]
        self.assertTrue(all(row["decision"] == "KEIN TRADE" for row in rows))
        self.assertTrue(all("Widersprüchliche aktuelle Social-Posts" in "; ".join(row["reasons"]) for row in rows))

    def test_old_opposite_post_does_not_block_current_catalyst(self):
        opposite = synthetic_post("BEARISH")
        opposite["timestamp"] = "2026-11-05T14:58:00+00:00"
        rows = social.analyze([self.post, opposite], self.evidence, self.cfg, social.timestamp(NOW))["results"]
        self.assertEqual([row["decision"] for row in rows], ["LONG", "KEIN TRADE"])

    def test_each_of_six_skills_is_required(self):
        for skill in social.SKILLS:
            with self.subTest(skill=skill):
                evidence = copy.deepcopy(self.evidence)
                del evidence["tickers"]["TSLA"]["skills"][skill]
                self.no_trade(evidence=evidence, contains=skill)

    def test_no_trade_with_insufficient_evidence(self):
        self.no_trade(evidence={"tickers": {}}, contains="Evidenz fehlt")

    def test_multiple_tickers_get_independent_decisions(self):
        self.post["affected_assets"].append({"symbol": "NVDA", "impact": "BEARISH", "basis": "Synthetic per-ticker reasoning"})
        rows = social.analyze([self.post], self.evidence, self.cfg, social.timestamp(NOW))["results"]
        self.assertEqual({r["ticker"]: r["decision"] for r in rows}, {"TSLA": "LONG", "NVDA": "KEIN TRADE"})
        self.assertEqual(rows[1]["impact"], "BEARISH")

    def test_repost_quote_reply_are_not_original_posts(self):
        for kind in ("repost", "quote", "reply", None):
            with self.subTest(kind=kind):
                self.post["content_kind"] = kind
                self.no_trade(contains="Original separat prüfen")

    def test_repost_does_not_refresh_original_age(self):
        self.post.update(content_kind="repost", original_timestamp="2026-11-05T14:58:00+00:00")
        row = self.no_trade(contains="Post zu alt")
        self.assertGreater(row["age_seconds"], 86400)

    def test_satire_and_parody_rejected(self):
        for flag in ("satire", "parody"):
            with self.subTest(flag=flag):
                post = copy.deepcopy(self.post)
                post[flag] = True
                self.assertEqual(self.no_trade(post)["verification"], "UNVERIFIED")

    def test_badge_and_caller_verified_label_are_not_proof(self):
        self.post["verification"] = "VERIFIED"
        self.post["blue_badge"] = True
        self.post["source"]["account_confirmed"] = False
        self.assertEqual(self.no_trade()["verification"], "UNVERIFIED")

    def test_unknown_or_disabled_account(self):
        self.post["account"] = "synthetic_parody"
        self.assertEqual(self.no_trade()["verification"], "UNVERIFIED")
        self.post["account"] = "elonmusk"
        self.cfg["accounts"][0]["enabled"] = False
        self.no_trade()

    def test_pinned_account_identity_mismatch(self):
        self.cfg["accounts"][0]["user_id"] = "43"
        self.assertEqual(self.no_trade()["verification"], "UNVERIFIED")

    def test_x_provenance_must_point_to_the_matching_api_resource(self):
        self.post["source"]["reference"] = "https://api.x.com/unrelated"
        self.assertEqual(self.no_trade()["verification"], "UNVERIFIED")

    def test_watchlist_association_alone_does_not_imply_ticker(self):
        self.post.pop("affected_assets")
        self.post["text"] = "SYNTHETIC OFFLINE TEST ONLY: no instrument mentioned"
        self.assertEqual(self.no_trade()["ticker"], "UNBEKANNT")

    def test_mentions_are_leads_and_missing_impact_basis_blocks(self):
        self.post.pop("affected_assets")
        row = self.no_trade(contains="Impact unklar")
        self.assertEqual(row["ticker"], "TSLA")
        self.post = synthetic_post()
        self.post["affected_assets"][0]["basis"] = ""
        self.no_trade(contains="Impact unklar")

    def test_mixed_impact_has_no_directional_trade(self):
        self.post["affected_assets"][0]["impact"] = "MIXED"
        self.no_trade(contains="Impact unklar oder gemischt")

    def test_commodities_indices_and_sectors_are_identified(self):
        self.post.pop("affected_assets")
        self.post["text"] = "SYNTHETIC OFFLINE TEST ONLY: WTI crude; S&P 500; electric vehicles"
        assets = social.affected(self.post, self.cfg)
        self.assertEqual({a["kind"] for a in assets}, {"Rohstoff", "Index", "Sektor"})


class MarketAndSkills(Offline):
    def test_stale_market_and_future_observation(self):
        for value in ("2026-11-06T14:50:00+00:00", "2026-11-06T15:01:00+00:00"):
            with self.subTest(value=value):
                self.evidence["tickers"]["TSLA"]["market"]["observed_at"] = value
                self.no_trade(contains="Kursreaktion fehlt")

    def test_stale_reaction_cannot_be_refreshed_by_a_new_quote(self):
        self.post["timestamp"] = "2026-11-06T14:45:00+00:00"
        market = self.evidence["tickers"]["TSLA"]["market"]
        market.update(before_at="2026-11-06T14:44:00+00:00", reaction_at="2026-11-06T14:46:00+00:00")
        self.no_trade(contains="Kursreaktion fehlt")

    def test_reaction_must_follow_the_post_and_baseline_must_precede_it(self):
        for field, value in (("reaction_at", POSTED), ("before_at", NOW)):
            with self.subTest(field=field):
                evidence = copy.deepcopy(self.evidence)
                evidence["tickers"]["TSLA"]["market"][field] = value
                self.no_trade(evidence=evidence, contains="nicht nach dem Post")

    def test_volume_and_spread_checks(self):
        for field, value, reason in (("volume", 1000, "Volumen bestätigt"), ("ask", 110, "Spread zu groß"),
                                     ("bid", None, "Spread unvollständig"), ("ask", 90, "Spread unvollständig")):
            with self.subTest(field=field):
                evidence = copy.deepcopy(self.evidence)
                evidence["tickers"]["TSLA"]["market"][field] = value
                self.no_trade(evidence=evidence, contains=reason)

    def test_invalid_market_numbers_cannot_confirm(self):
        for value in (float("nan"), float("inf"), -1, 0, True, "not-a-number"):
            with self.subTest(value=value):
                self.evidence["tickers"]["TSLA"]["market"]["price_after"] = value
                self.no_trade(contains="ungültig")

    def test_unequal_or_missing_volume_windows_cannot_confirm(self):
        for value in (None, 0, 300):
            with self.subTest(value=value):
                self.evidence["tickers"]["TSLA"]["market"]["baseline_window_seconds"] = value
                self.no_trade(contains="Volumenfenster")

    def test_trading_time_and_halts(self):
        for field, value in (("halted", True), ("halted", None), ("trading_open", False), ("session", "PREMARKET")):
            with self.subTest(field=field, value=value):
                evidence = copy.deepcopy(self.evidence)
                evidence["tickers"]["TSLA"]["market"][field] = value
                self.no_trade(evidence=evidence, contains="Handelszeit oder Halt")

    def test_exchange_calendar_rejects_weekend_even_when_input_says_open(self):
        row = self.no_trade(now="2026-11-07T15:00:00+00:00", contains="Kalender geschlossen")
        self.assertEqual(row["price_confirmation"], "NEIN")

    def test_risk_block_and_stale_or_unreviewed_skill_evidence(self):
        for field, value in (("status", "BLOCK"), ("checked_at", "2026-11-06T14:00:00+00:00"),
                             ("checked_at", "2026-11-06T15:01:00+00:00"), ("reviewed", False)):
            with self.subTest(field=field):
                evidence = copy.deepcopy(self.evidence)
                evidence["tickers"]["TSLA"]["skills"]["trader-risk"][field] = value
                self.no_trade(evidence=evidence, contains="trader-risk")

    def test_mismatched_direction_setup_and_risk_prices(self):
        for skill, field, value, reason in (
                ("trader-entry", "side", "SHORT", "Setup-Richtung"),
                ("trader-risk", "setup_id", "other-setup", "selben Setup"),
                ("trader-risk", "stop", 90, "nicht konsistent"),
                ("trader-exit", "stop", 103, "nicht konsistent"),
                ("trader-entry", "within_entry_window", False, "Entry-Zeitfenster")):
            with self.subTest(skill=skill, field=field):
                evidence = copy.deepcopy(self.evidence)
                evidence["tickers"]["TSLA"]["skills"][skill][field] = value
                self.no_trade(evidence=evidence, contains=reason)

    def test_opposed_sentiment_blocks_trade(self):
        self.evidence["tickers"]["TSLA"]["skills"]["trader-sentiment"]["side"] = "SHORT"
        self.no_trade(contains="Sentiment widerspricht")

    def test_news_must_explicitly_exclude_conflicts(self):
        del self.evidence["tickers"]["TSLA"]["skills"]["trader-news"]["conflict"]
        self.no_trade(contains="News-Konflikte nicht ausdrücklich ausgeschlossen")


class NativePaperPipeline(Offline):
    def native_evidence(self, side="buy", kind="stock"):
        # Reuse PR #2's synthetic bars/terms and run the real native pipeline.
        # No broker, order file or connector is used; calculators run offline.
        from test_paper_pipeline import full_document, STAMP
        with patch("socket.socket.connect", side_effect=AssertionError("offline only")):
            document = full_document(side, kind)
            candidate = social.local_tool("trader-sentiment", "sentiment").analyze(document, STAMP)["candidates"][0]
        expected = "LONG" if side == "buy" else "SHORT"
        self.assertEqual(candidate["decision"], expected, candidate["reasons"])
        self.assertEqual(candidate["risk"]["results"][0]["verdict"], "PASS")
        native = document["candidates"][0]
        plan = native["context"]["exit"]
        symbol = native["symbol"]
        post = synthetic_post("BULLISH" if side == "buy" else "BEARISH", symbol=symbol)
        post["text"] = "SYNTHETIC OFFLINE TEST ONLY: reviewed instrument mapping"
        now = social.timestamp(STAMP)
        post["timestamp"] = (now - datetime.timedelta(minutes=2)).isoformat()
        evidence = synthetic_evidence(expected, symbol)
        record = evidence["tickers"][symbol]
        record["instrument"] = copy.deepcopy(plan)
        record["market"].update(
            before_at=(now - datetime.timedelta(minutes=3)).isoformat(),
            reaction_at=STAMP, observed_at=STAMP, symbol=symbol, instrument_type=kind,
            currency="USD", price_basis="mid", mapping_verified=True, same_price_basis=True)
        if kind == "cfd":
            record["market"]["contract"] = copy.deepcopy(plan["contract"])
        for skill, report in record["skills"].items():
            report.update(side=side, checked_at=STAMP, symbol=symbol, instrument_type=kind,
                          currency="USD", price_basis="mid")
            report.pop("short_capable", None)
            if kind == "cfd":
                report["contract"] = copy.deepcopy(plan["contract"])
            if skill in ("trader-entry", "trader-exit", "trader-risk"):
                report["entry"] = plan["entry"]
            if skill in ("trader-exit", "trader-risk"):
                report.update(stop=plan["stop"], target=plan["target"])
        record["skills"]["trader-risk"].update(analysis_only=True, paper=True)
        record["skills"]["trader-sentiment"]["native_result"] = candidate
        self.cfg["allow_short_analysis"] = True
        return post, evidence, STAMP

    def assert_native_candidate(self, side, kind):
        post, evidence, now = self.native_evidence(side, kind)
        row = self.row(post, evidence, now)
        self.assertEqual(row["decision"], "LONG" if side == "buy" else "SHORT", row["reasons"])
        self.assertEqual(row["instrument"].get("instrument_type", "stock"), kind)
        if kind == "cfd":
            self.assertEqual(row["instrument"]["contract"], evidence["tickers"][row["ticker"]]["instrument"]["contract"])

    def test_actual_native_stock_long(self):
        self.assert_native_candidate("buy", "stock")

    def test_actual_native_stock_short(self):
        self.assert_native_candidate("sell", "stock")

    def test_actual_native_cfd_long(self):
        self.assert_native_candidate("buy", "cfd")

    def test_actual_native_cfd_short(self):
        self.assert_native_candidate("sell", "cfd")

    def test_cfd_underlying_quotes_cannot_replace_contract_quotes(self):
        post, evidence, now = self.native_evidence("sell", "cfd")
        evidence["tickers"][post["affected_assets"][0]["symbol"]]["market"]["symbol"] = "TSLA"
        self.no_trade(post, evidence, "CFD-Vertragsidentität", now)

    def test_cfd_contract_mismatch_in_each_native_report_blocks(self):
        post, evidence, now = self.native_evidence("sell", "cfd")
        symbol = post["affected_assets"][0]["symbol"]
        for skill in ("trader-scanner", "trader-entry", "trader-exit", "trader-risk", "trader-sentiment"):
            with self.subTest(skill=skill):
                changed = copy.deepcopy(evidence)
                changed["tickers"][symbol]["skills"][skill]["contract"]["contract_size"] = 99
                self.no_trade(post, changed, "CFD-Vertragsidentität", now)

    def test_cfd_missing_descriptor_and_invalid_contract_terms_block(self):
        post, evidence, now = self.native_evidence("buy", "cfd")
        symbol = post["affected_assets"][0]["symbol"]
        missing = copy.deepcopy(evidence)
        del missing["tickers"][symbol]["instrument"]
        self.no_trade(post, missing, "CFD-Instrumentdescriptor", now)
        evidence["tickers"][symbol]["instrument"]["contract"]["margin_rate"] = 0.5
        self.no_trade(post, evidence, "Exposure-Prüfung", now)

    def test_cfd_requires_paper_analysis_flags(self):
        post, evidence, now = self.native_evidence("sell", "cfd")
        symbol = post["affected_assets"][0]["symbol"]
        evidence["tickers"][symbol]["skills"]["trader-risk"]["paper"] = False
        self.no_trade(post, evidence, "Paper-/Analyse-Evidenz", now)

    def test_native_sentiment_no_trade_cannot_be_relabelled_aligned(self):
        post, evidence, now = self.native_evidence("sell", "stock")
        evidence["tickers"]["TSLA"]["skills"]["trader-sentiment"]["native_result"]["decision"] = "KEIN TRADE"
        self.no_trade(post, evidence, "trader-sentiment bestätigt", now)


class OfficialX(Offline):
    def account(self):
        return self.cfg["accounts"][0]

    def responses(self, references=None):
        row = {"id": "100", "author_id": "42", "created_at": POSTED,
               "text": "SYNTHETIC OFFLINE API RESPONSE ONLY: $TSLA"}
        if references:
            row["referenced_tweets"] = references
        return [{"data": {"id": "42", "username": "elonmusk", "name": "Synthetic offline account label"}},
                {"data": [row], "meta": {"next_token": "synthetic-next-page"}}]

    def test_missing_x_token_has_zero_requests(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(x_api, "_get") as get:
            result = x_api.fetch_account(self.account())
        get.assert_not_called()
        self.assertEqual(result["status"], "DATA UNAVAILABLE")
        self.assertEqual(result["posts"], [])

    def test_official_v2_lookup_and_timeline_only(self):
        with patch.dict(os.environ, {"X_BEARER_TOKEN": "SYNTHETIC-OFFLINE-TOKEN"}), \
                patch.object(x_api, "_get", side_effect=self.responses()) as get:
            result = x_api.fetch_account(self.account())
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[0].args[0], "/2/users/by/username/elonmusk")
        self.assertTrue(get.call_args_list[1].args[0].startswith("/2/users/42/tweets?"))
        self.assertEqual(result["posts"][0]["timestamp"], POSTED)
        self.assertEqual(result["posts"][0]["content_kind"], "original")
        self.assertTrue(result["partial"])
        self.assertNotIn("SYNTHETIC-OFFLINE-TOKEN", json.dumps(result))

    def test_empty_timeline_is_valid_only_when_api_reports_zero_results(self):
        for timeline, status in (({"meta": {"result_count": 0}}, "OK"), ({}, "DATA UNAVAILABLE")):
            with self.subTest(timeline=timeline), patch.dict(os.environ, {"X_BEARER_TOKEN": "SYNTHETIC-OFFLINE-TOKEN"}), \
                    patch.object(x_api, "_get", side_effect=[self.responses()[0], timeline]):
                result = x_api.fetch_account(self.account())
                self.assertEqual(result["status"], status)
                self.assertEqual(result["posts"], [])

    def test_reposts_quotes_and_replies_preserve_reference(self):
        for kind, expected in (("retweeted", "repost"), ("quoted", "quote"), ("replied_to", "reply")):
            with self.subTest(kind=kind), patch.dict(os.environ, {"X_BEARER_TOKEN": "SYNTHETIC-OFFLINE-TOKEN"}), \
                    patch.object(x_api, "_get", side_effect=self.responses([{"type": kind, "id": "99"}])):
                post = x_api.fetch_account(self.account())["posts"][0]
                self.assertEqual(post["content_kind"], expected)
                self.assertEqual(post["referenced_posts"][0]["id"], "99")

    def test_truth_social_and_invalid_accounts_never_requested(self):
        for account in (self.cfg["accounts"][1], dict(self.account(), account="../truthsocial.com"),
                        dict(self.account(), enabled=False)):
            with self.subTest(account=account), patch.dict(os.environ, {"X_BEARER_TOKEN": "SYNTHETIC-OFFLINE-TOKEN"}), \
                    patch.object(x_api, "_get") as get:
                self.assertEqual(x_api.fetch_account(account)["status"], "DATA UNAVAILABLE")
                get.assert_not_called()

    def test_http_auth_rate_limit_and_network_failures_do_not_leak_secrets(self):
        errors = [urllib.error.HTTPError("https://api.x.com", code, "SYNTHETIC-OFFLINE-TOKEN", {}, None)
                  for code in (401, 403, 429, 500)] + [OSError("SYNTHETIC-OFFLINE-TOKEN")]
        for error in errors:
            with self.subTest(error=type(error).__name__), \
                    patch.dict(os.environ, {"X_BEARER_TOKEN": "SYNTHETIC-OFFLINE-TOKEN"}), \
                    patch("urllib.request.build_opener") as build:
                build.return_value.open.side_effect = error
                result = x_api.fetch_account(self.account())
                self.assertEqual(result["status"], "DATA UNAVAILABLE")
                self.assertNotIn("SYNTHETIC-OFFLINE-TOKEN", json.dumps(result))

    def test_author_identity_and_missing_timestamp_fail_closed(self):
        for field, value in (("author_id", "43"), ("created_at", None), ("id", "bad-id")):
            replies = self.responses()
            replies[1]["data"][0][field] = value
            with self.subTest(field=field), patch.dict(os.environ, {"X_BEARER_TOKEN": "SYNTHETIC-OFFLINE-TOKEN"}), \
                    patch.object(x_api, "_get", side_effect=replies):
                self.assertEqual(x_api.fetch_account(self.account())["status"], "DATA UNAVAILABLE")

    def test_invalid_or_incomplete_api_json_is_unavailable(self):
        for raw in (b"not-json", b'{"errors": [{"title": "synthetic"}]}', b'[]'):
            response = Mock()
            response.read.return_value = raw
            with self.subTest(raw=raw), patch("urllib.request.build_opener") as build:
                build.return_value.open.return_value.__enter__.return_value = response
                with self.assertRaises(x_api.XUnavailable):
                    x_api._get("/2/users/by/username/elonmusk", "SYNTHETIC-OFFLINE-TOKEN")

    def test_api_request_uses_fixed_official_origin_and_bearer_header(self):
        response = Mock()
        response.read.return_value = b'{"data": {}}'
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.return_value.__enter__.return_value = response
            x_api._get("/2/users/by/username/elonmusk", "SYNTHETIC-OFFLINE-TOKEN")
            req = build.return_value.open.call_args.args[0]
            self.assertEqual(req.full_url, "https://api.x.com/2/users/by/username/elonmusk")
            self.assertEqual(req.get_header("Authorization"), "Bearer SYNTHETIC-OFFLINE-TOKEN")
            self.assertIsInstance(build.call_args.args[0], x_api.NoRedirect)

    def test_redirects_to_any_domain_are_refused(self):
        for url in ("https://x.com", "https://truthsocial.com", "https://api.x.com/other"):
            with self.subTest(url=url), self.assertRaises(x_api.XUnavailable):
                x_api.NoRedirect().redirect_request(None, None, 302, "Found", {}, url)


class CommandAndConfig(Offline):
    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = social.main(args)
        return code, out.getvalue(), err.getvalue()

    def write(self, folder, name, data):
        path = os.path.join(folder, name)
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
        return path

    def test_json_cli_combines_evidence_and_never_writes_orders(self):
        with tempfile.TemporaryDirectory() as folder:
            posts = self.write(folder, "posts.json", [self.post])
            evidence = self.write(folder, "evidence.json", self.evidence)
            before = sorted(os.listdir(folder))
            code, out, err = self.run_cli(["analyze", "--posts", posts, "--evidence", evidence, "--now", NOW, "--json"])
            self.assertEqual(code, 0, err)
            row = json.loads(out)["results"][0]
            self.assertEqual(row["decision"], "LONG")
            self.assertEqual(sorted(os.listdir(folder)), before)
            self.assertEqual(row["text"], self.post["text"])

    def test_default_analysis_no_evidence_returns_required_table(self):
        with tempfile.TemporaryDirectory() as folder:
            posts = self.write(folder, "posts.json", {"posts": [self.post]})
            code, out, err = self.run_cli(["analyze", "--posts", posts, "--now", NOW])
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith(social.HEADER))
        self.assertIn("KEIN TRADE", out)
        self.assertIn("trader-sentiment", out)

    def test_missing_token_cli_is_data_unavailable(self):
        with patch.dict(os.environ, {}, clear=True):
            code, out, _ = self.run_cli(["x"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["status"], "DATA UNAVAILABLE")

    def test_truth_social_cli_does_not_fetch(self):
        code, _, err = self.run_cli(["x", "--account", "realDonaldTrump"])
        self.assertEqual(code, 2)
        self.assertIn("KEIN TRADE", err)

    def test_invalid_inputs_fail_closed_without_echoing_content(self):
        for data in (None, "SYNTHETIC-SECRET", [None], [{"source": "bad"}]):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as folder:
                posts = self.write(folder, "posts.json", data)
                code, out, err = self.run_cli(["analyze", "--posts", posts, "--now", NOW])
                self.assertEqual(code, 2)
                self.assertNotIn("SYNTHETIC-SECRET", out + err)

    def test_data_unavailable_import_is_no_trade(self):
        with tempfile.TemporaryDirectory() as folder:
            posts = self.write(folder, "posts.json", {"status": "DATA UNAVAILABLE", "posts": []})
            code, out, _ = self.run_cli(["analyze", "--posts", posts])
        self.assertEqual(code, 1)
        self.assertIn("KEIN TRADE", out)

    def test_unavailable_json_import_preserves_machine_readable_output(self):
        with tempfile.TemporaryDirectory() as folder:
            posts = self.write(folder, "posts.json", {"status": "DATA UNAVAILABLE", "posts": []})
            code, out, _ = self.run_cli(["analyze", "--posts", posts, "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["decision"], "KEIN TRADE")

    def test_thresholds_can_be_configured(self):
        self.cfg["min_relative_volume"] = 4
        self.no_trade(contains="Volumen bestätigt")

    def test_invalid_settings_and_social_hosts_are_rejected(self):
        cases = [("mode", "live"), ("max_post_age_minutes", 0), ("max_spread_pct", float("nan")),
                 ("allow_short_analysis", "true"), ("allowed_news_hosts", ["api.truthsocial.com"]),
                 ("allowed_official_hosts", ["x.com"])]
        for key, value in cases:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as folder:
                cfg = copy.deepcopy(self.cfg)
                cfg[key] = value
                path = self.write(folder, "watchlist.json", cfg)
                with self.assertRaises(social.SocialError):
                    social.config(path)

    def test_timestamp_offsets_describe_the_same_instant(self):
        self.post["timestamp"] = "2026-11-06T09:58:00-05:00"
        self.assertEqual(self.row()["age_seconds"], 120)
        self.assertEqual(self.row()["timezone"], "-0500")

    def test_helpers_parse_with_python38_grammar_and_have_no_broker_import(self):
        for name in ("social.py", "x_api.py"):
            with open(os.path.join(ROOT, "trader-social", name), encoding="utf-8") as stream:
                source = stream.read()
            ast.parse(source, feature_version=(3, 8))
            self.assertNotIn("trader-broker", source)


if __name__ == "__main__":
    unittest.main()
