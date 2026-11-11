#!/usr/bin/env python3
"""Coverage, source-integrity audit and a report for the expanded daily study."""
import argparse
import collections
import csv
import gzip
import hashlib
import heapq
from pathlib import Path

import numpy as np

from common import digest, read, write
from daily_history import BITS, HORIZONS, NAMES, mapped
from daily_results import forward_vector
from reporting import fmt, table
from run import fingerprints


def file_hash(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def intraday_inventory(first, universe, output):
    """Inventory existing data only; absent files are not provider rejections."""
    document = read(Path(first) / "data/dataset.json")
    original = read(Path(first) / "results/signals.json")
    rows = []
    for sym in universe:
        frames = document.get("bars", {}).get(sym, {})
        intra = frames.get("1m") or frames.get("5m") or []
        by_day = collections.defaultdict(list)
        for bar in intra:
            by_day[bar["start"][:10]].append(bar)
        complete = 0
        for day, bars in by_day.items():
            hours = document["sessions"].get(day)
            if not hours:
                continue
            minutes = 1 if "1m" in frames else 5
            open_h, open_m = map(int, hours[0].split(":"))
            close_h, close_m = map(int, hours[1].split(":"))
            expected = ((close_h - open_h) * 60 + close_m - open_m) // minutes
            starts = sorted(b["start"] for b in bars)
            # First-run MarketData already validated timestamp durations.
            from common import stamp
            contiguous = all((stamp(b) - stamp(a)).total_seconds() == minutes * 60 for a, b in zip(starts[:-1], starts[1:]))
            if len(set(starts)) == expected and contiguous:
                complete += 1
        rows.append({"ticker": sym, "intraday_bars": len(intra), "sessions": len(by_day), "complete_sessions": complete,
                     "first": min(by_day) if by_day else None, "last": max(by_day) if by_day else None,
                     "status": "EXISTING_PILOT_DATA" if intra else "NOT_COLLECTED_FOR_INTRADAY",
                     "exclusion_reason": None if intra else "DAILY_EXPANSION_REQUESTED_DAILY_DATA_ONLY; no inference about provider availability"})
    with (output / "intraday-coverage.csv").open("w", newline="") as stream:
        w = csv.DictWriter(stream, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    triggers = {name: sum(s["strategy"] == name and s["status"] == "TRIGGERED" for s in original["signals"])
                for name in ("QULLAMAGGIE_BREAKOUT", "EPISODIC_PIVOT", "PARABOLIC_SHORT", "PARABOLIC_LONG", "CONFLUENCE_TRIGGERED")}
    return {"scope": "Existing eight-stock pilot only; its ranks used its own eight-symbol population, not the expanded daily universe",
        "adequate_stocks": sum(r["complete_sessions"] > 0 for r in rows),
        "not_collected_stocks": sum(not r["intraday_bars"] for r in rows),
        "first": min(r["first"] for r in rows if r["first"]), "last": max(r["last"] for r in rows if r["last"]),
        "sessions_per_collected_stock": sorted({r["sessions"] for r in rows if r["sessions"]}),
        "pilot_structural_trigger_observations": triggers,
        "ep_limitation": "No historical catalyst archive; zero confirmed observations does not establish absence of EP opportunities",
        "provider_limit": "Saved Yahoo HTTP 422 says requested 5m range must be within last 60 days",
        "daily_exclusions_due_to_intraday": 0}


def extreme_audit(analysis, meta):
    shape = meta["shape"]
    close = mapped(analysis / "close.f64", shape, "float64")
    after = mapped(analysis / "forward-contiguous.u8", shape, "uint8")
    signals = mapped(analysis / "signals.u8", shape, "uint8")
    tops = {name: [] for name in NAMES}
    for day in range(meta["test_first_index"], meta["test_last_index"] + 1):
        returns = forward_vector(close, after, day, 20, meta["test_last_index"])
        for name, bit in zip(NAMES, BITS):
            indices = np.flatnonzero(((signals[day] & bit) != 0) & np.isfinite(returns) & (returns > 1000))
            for i in indices:
                item = (float(returns[i]), meta["days"][day], meta["universe"][i], float(close[day, i]), float(close[day + 20, i]))
                heapq.heappush(tops[name], item)
                if len(tops[name]) > 10:
                    heapq.heappop(tops[name])
    return {name: [dict(return_pct=r, day=d, ticker=s, start_price=c, end_price=e) for r, d, s, c, e in sorted(entries, reverse=True)]
            for name, entries in tops.items()}


def report(analysis, data, output, first, classification):
    analysis, data, output = Path(analysis), Path(data), Path(output)
    meta = read(analysis / "matrix-metadata.json")
    source = read(output / "daily-source-statistics.json")
    random = read(output / "random-controls.json")
    coverage = read(analysis / "ticker-coverage.json")
    universe = read(classification)
    manifest = read(data / "acquisition.json")
    expected = read(Path(__file__).parent / "strategy-sources.json")
    if fingerprints() != expected["sha256"]:
        raise ValueError("strategy sources changed from the recorded origin/main baseline")
    exclusions = collections.Counter(r.get("daily_exclusion") for r in coverage)
    with (output / "ticker-coverage-and-exclusions.csv").open("w", newline="") as stream:
        w = csv.writer(stream)
        w.writerow(["ticker", "fetch_status", "fetch_error", "valid_daily_sessions", "eligible_sessions", "first_eligible", "last_eligible", "data_exclusion"])
        for row in coverage:
            w.writerow([row["ticker"], row["status"], row.get("error"), row.get("valid_sessions", 0), row.get("common_eligible_sessions", 0),
                        row.get("first_eligible"), row.get("last_eligible"), row.get("daily_exclusion")])
    intra = intraday_inventory(first, meta["universe"], output)
    extremes = extreme_audit(analysis, meta)
    write(output / "extreme-returns-20d.json", extremes)
    write(output / "intraday-inventory.json", intra)
    from importlib.metadata import version
    files = ["close.f64", "eligible.u8", "forward-contiguous.u8", "ranks.f64", "signals.u8", "matrix-metadata.json", "ticker-coverage.json", "native-call-audit.json"]
    verification = {"strategy_source_revision": expected["origin_main"], "strategy_source_sha256": fingerprints(),
        "analysis_artifact_sha256": {name: file_hash(analysis / name) for name in files},
        "input_manifest_sha256": digest(manifest), "calendar_sha256": meta["calendar_sha256"],
        "backtest_source_sha256": {p.name: file_hash(p) for p in sorted(Path(__file__).parent.glob("*.py"))},
        "runtime": {"numpy": version("numpy"), "pandas": version("pandas")},
        "daily_intraday_separated": True, "intraday_required_for_daily": False}
    write(output / "verification.json", verification)
    combined = {"statistics": source, "random_controls": random, "intraday_inventory": intra,
                "coverage_exclusions": dict((str(k), v) for k, v in exclusions.items()), "extreme_20d_returns": extremes,
                "verification": verification}
    write(output / "report.json", combined)
    cohort = source["daily_cohort"]
    lines = ["# Erweiterter DAILY-HISTORY-BACKTEST", "",
        "Abgeschlossene Tageskerzen, unveraenderte Strategiefunktionen und eingefrorener Datensnapshot. Keine Intraday-Voraussetzung, keine Strategieoptimierung und keine Order-/Brokerfunktion.", "",
        "## Wichtigste Vergleichswerte", "",
        "Zeitraum: **%s bis %s**, %d Handelssitzungen. Von **%d** angefragten US-gelisteten Aktien/ADRs waren **%d** an mindestens einem Testtag auswertbar. Dieselben %d Ticker bilden fuer alle Varianten das Ausgangsuniversum; an jedem Testtag wird dieselbe damalige Datenberechtigung verwendet." % (
            cohort["start"], cohort["end"], cohort["sessions"], cohort["requested"], cohort["used_stocks"], cohort["requested"]), "",
        "**20 weitere Handelstage, Brutto-Preisrendite ab Tages-Schlusskurs:**", ""]
    rows = []
    for name in ("MINERVINI", "BREAKOUT_SETUP_READY", "CONFLUENCE_SETUP_READY"):
        f = source["periods"]["full"]["strategies"][name]["20"]
        rows.append([name, source["signal_counts"][name]["signals"], f["n"], fmt(f["mean_return_pct"]), fmt(f["median_return_pct"]), fmt(f["win_rate"], percent=True), fmt(f["excess_return_vs_spy_pct"])])
    lines += table(["Variante", "Signale", "auswertbares n", "Mean %", "Median %", "Win %", "Excess vs. SPY pp"], rows)
    lines += ["", "CANDIDATE und SETUP_READY werden durchgehend getrennt ausgewiesen. SETUP_READY ist kein ausgefuehrter Entry. Der Daily-Test misst Signalqualitaet, keine realisierte Trade- oder Portfolio-Netto-Performance.", "",
        "## Warum der erste Lauf nur acht Aktien enthielt", "",
        "Die acht Aktien waren eine von Beginn an explizit konfigurierte Projektauswahl. Ein groesseres Universum wurde damals nicht angefragt. Alle acht Daily- und Intraday-Dateien wurden geladen; kein Beschaffungsfilter reduzierte eine groessere Stichprobe auf acht. 5.925 der heute scannerberechtigten 5.933 Aktien wurden im Pilot gar nicht angefragt.", "",
        "Die urspruengliche Stichprobe war fuer den gewuenschten Strategievergleich zu eng. Ausserdem wurden 14 der 51 Breakout-SETUP_READY-Beobachtungen des Piloten zuerst intraday erfasst. Der neue Daily-Test hat deshalb einen eigenen, ausschliesslich am Tages-Schlusskurs verankerten Signalstrom. Siehe [reine Diagnose](../../coverage-diagnosis/DIAGNOSIS.md).", "",
        "## Datenabdeckung und Ausschlussstufen", ""]
    loaded = sum(r["status"] == "loaded" for r in coverage)
    compatible = loaded - sum(v for k, v in exclusions.items() if k not in (None, "DAILY_FETCH_FAILED", "NO_COMPLETE_260_SESSION_WINDOW"))
    rows = [
        ["Nasdaq-Verzeichniszeilen", universe["counts"]["rows"], "Zwei gespeicherte Verzeichnisse, Stand " + universe["created"]],
        ["Unveraenderte Scanner-Klassifikation", len(meta["universe"]), "%d ETFs, Test-Issues, Warrants, Units, ungueltige Ticker usw. ausgeschlossen" % (universe["counts"]["rows"] - len(meta["universe"]))],
        ["Erfolgreiche Daily-Anfragen (ohne SPY/QQQ)", loaded, "HYAC: EMPTY_HISTORY; TXCP: HTTP 404 / no data"],
        ["Passende Equity-/USD-Tagesdaten", compatible, "BAR: Provider klassifiziert das Instrument als Nicht-EQUITY"],
        ["Mindestens ein gueltiges 260-Sitzungen-Fenster", cohort["used_stocks"], "%d ohne vollstaendiges Fenster: kurze Historie oder Luecken" % exclusions["NO_COMPLETE_260_SESSION_WINDOW"]],
        ["Zeitpunktbezogene gemeinsame Datenkohorte", "%d bis %d; Median %.0f" % (cohort["minimum"], cohort["maximum"], cohort["median"]), "Nur zu diesem Datum abgeschlossene, lueckenlose Historie; keine spaetere Aufnahme vorweggenommen"],
        ["Daily-Ausschluss wegen fehlender Intraday-Daten", 0, "Intraday wird im Daily-Layer nicht gelesen"]]
    lines += table(["Stufe", "Verbleibend / Anzahl", "Grund und Umfang"], rows)
    lines += ["", "Vollstaendige Ticker-/Grundlisten: [ticker-coverage-and-exclusions.csv](ticker-coverage-and-exclusions.csv), [zeitliche Berechtigungsintervalle](../analysis/daily-eligibility-intervals.csv) und [urspruengliche Auswahl](../../coverage-diagnosis/pilot-exclusion-ledger.csv). Die Tageskohorte und Signalzahlen stehen in daily-cohort-and-signal-counts.csv. Ein Strategie-FAIL entfernt keinen Ticker dauerhaft aus dem Universum.", "",
        "Die Historie beginnt beim ersten gemeinsamen SPY-Tag mit mindestens zwei datenberechtigten Aktien. Aeltere Tagesdaten dienen der Indikator-Aufwaermphase. Die gespeicherte XNYS-Sitzungsliste beruecksichtigt Sonder-Schliessungen und historische Sommerzeitregeln. Enddatum bleibt der vor der Unterbrechung eingefrorene 06.11.2026; es wurde kein neuer Snapshot vom 10.11. nachgeladen.", "",
        "## Regeln, Zeitpunkt und Kosten", "",
        "Minervini: natives Trend Template PASS. Breakout: native Funktionen fuer CANDIDATE und SETUP_READY mit unveraenderten Schwellen. Confluence: natives Minervini-PASS plus jeweiliger Breakout-Status. Querschnittsrangfolgen werden vor jeder Signalauswahl auf der gemeinsamen 260-Sitzungen-Kohorte berechnet. Die zusaetzliche gemeinsame Datenberechtigung vereinheitlicht den Vergleich; der native Breakout benoetigt fuer sich nur 127 Tage. Parameter wie Leader-Perzentil, Base-Laenge und Kontraktion bleiben unveraendert.", "",
        "Nur abgeschlossene Tageskerzen. Keine aktuellen Quote-Metadaten, Intraday-Trigger, Meldungen oder spaeter veroeffentlichten Ereignisse als Daily-Feature. Spaetere Splits werden ausschliesslich zur Ruecknahme heutiger Provider-Preisbereinigung verwendet; sie erzeugen kein Signalmerkmal. Tages-Schlusskurs als deskriptiver Referenzpreis, keine Behauptung eines exakt dort ausfuehrbaren Trades.", "",
        "Forward Returns sind splitbereinigte Preisrenditen ohne Dividenden. Fuer alle Strategien und Zufallskontrollen gelten dieselben Brutto-Annahmen: keine erfundenen Entries und keine Ausfuehrungskosten. Spread/Slippage/Fee-Sensitivitaet bleibt im separaten Trade-/Intraday-Layer. Fehlende Forward-Kerzen werden gezaehlt, nicht zu Null-Renditen gemacht und nicht uebersprungen.", "",
        "## Alle Forward-Horizonte", ""]
    for label, p in source["periods"].items():
        lines += ["### " + label + ": " + p["start"] + " bis " + p["end"], ""]
        rows = []
        for h in HORIZONS:
            for name in NAMES:
                f = p["strategies"][name][str(h)]
                rows.append([h, name, f["signals"], f["n"], fmt(f["mean_return_pct"]), fmt(f["median_return_pct"]),
                    fmt(f["win_rate"], percent=True), fmt(f["excess_return_vs_spy_pct"]), f["right_or_boundary_censored"], f["missing_forward_history"]])
        lines += table(["Tage", "Variante", "Signale", "n", "Mean %", "Median %", "Win %", "Excess SPY pp", "Ende/Grenze zensiert", "Forward-Luecken"], rows) + [""]
    lines += ["## In-Sample und Out-of-Sample", "",
        "Erste 70 % der Sitzungstage: In-Sample; letzte 30 %: zeitlicher Holdout. Gleiche Parameter in beiden Abschnitten, keine Anpassung anhand des Holdouts. Forward-Horizonte, die eine Abschnittsgrenze ueberschreiten, sind zensiert. Historische Kursmerkmale aus dem jeweils bereits vergangenen Abschnitt bleiben zulaessig.", "",
        "Das ist ein zeitlicher Holdout unter rueckblickender Auswahl des heutigen Universums. Es ist kein echter Point-in-Time-OOS-Nachweis. Fuer die Interpretation gelten Survivorship- und Revisionsrisiko in beiden Abschnitten.", "",
        "## Random Baseline: 1.000 Simulationen", "",
        "Pro Simulation und Signaldatum wird ohne Ticker-Duplikate eine uniforme Teilmenge aus derselben damals datenberechtigten Kohorte gezogen. Anzahl ausgewaehlter Beobachtungen, Tage, Horizonte und Brutto-Kostenannahmen entsprechen der jeweiligen Strategie. Die Varianten teilen sich Zufallspermutationen; jede k-Teilmenge ist fuer sich gleichverteilt. Seed 20261109; fortsetzbare Checkpoints.", "",
        "Es erfolgt keine Auswahl nach spaeter verfuegbaren Forward-Kursen. Fehlende Zukunftskurse bleiben fehlend. Wie bei der Strategie wird der Mean aus beobachtbaren Outcomes berechnet; die daraus geringfuegig unterschiedlichen beobachtbaren Sample Sizes sind im JSON vollstaendig ausgewiesen. Ausgewaehlte Beobachtungszahlen bleiben gleich. Die Kontrolle ist eine Signalqualitaets-Benchmark, kein investierbares Portfolio mit Cash, Slots oder Netto-P&L.", "",
        "**Gesamter Zeitraum:** Median und P05/P95 beziehen sich auf den mittleren Return jeder Zufallssimulation, nicht auf den Median einzelner Aktienreturns.", ""]
    rows = []
    for h in HORIZONS:
        for name in NAMES:
            r = random["full"][name][str(h)]
            rows.append([h, name, r["valid_mean_estimates"], fmt(r["median_random_mean_return_pct"]),
                fmt(r["p05_random_mean_return_pct"]), fmt(r["p95_random_mean_return_pct"]), fmt(r["fraction_random_better"], percent=True),
                "%s / %s" % (r["random_observed_outcomes_min"], r["random_observed_outcomes_max"])])
    lines += table(["Tage", "Variante", "Laeufe", "Zufall Median %", "P05 %", "P95 %", "Zufall besser %", "beobachtbares n min/max"], rows) + [""]
    lines += ["IS/OOS-Zufallsergebnisse stehen separat in random-controls.json und report.json. Der Anteil besserer Zufallslaeufe ist eine deskriptive Kontrolle, kein p-Wert und kein Beweis fuer oder gegen einen Edge.", "",
        "## Ausreisser und Aussagekraft", "",
        "Die gespeicherte Quelle enthaelt extreme Bewegungen. Beispiel PPCB: vom Tages-Schlusskurs 0,15 auf 2.500 innerhalb von 20 Sitzungen, rund 1.666.567 %. Das ist ein abklaerungsbeduerftiger Corporate-Action-/Kursbasisfall. Ohne unabhaengige historische Instrumenten- und Kapitalmassnahmendaten wird er weder als echte Rendite bestaetigt noch stillschweigend korrigiert oder entfernt.", "",
        "Zwei PPCB-Beobachtungen tragen rund 5,17 Prozentpunkte zum 20-Tage-CANDIDATE-Mittelwert von 6,11 % bei. Auch die breite Zufallskontrolle kann solche Werte ziehen; ihre Mean-Verteilung ist dadurch stark verzerrbar. Die Haupttabellen bleiben unveraendert. Die groessten beobachteten Werte sind in extreme-returns-20d.json dokumentiert.", "",
        "Belastbarer als der Pilot sind die dokumentierte Datenabdeckung, der Vergleich derselben breiten Tageskohorte, die exakt berechneten Verteilungskennzahlen auf diesem Snapshot und die reproduzierbaren Rechenregeln. Millionen ueberlappende Signale sind keine Millionen unabhaengiger Trades. Block-Bootstrap-Intervalle gruppieren ganze Signaltage und verwenden Bloecke in Laenge des jeweiligen Forward-Horizonts; die Intervalle stehen im JSON und beheben keine Daten- oder Universumsverzerrung.", "",
        "Strategie-Edge, Netto-Profitabilitaet und die genaue Hoehe der Mean- und Zufallsrenditen bleiben explorativ. Kennzeichnungen: SURVIVORSHIP_BIAS_RISK, REVISED_DATA_NOT_POINT_IN_TIME, CURRENT_DIRECTORY_NOT_HISTORICAL_MEMBERSHIP und NO_HISTORICAL_SECURITY_IDENTIFIER_ARCHIVE. Delistete Unternehmen, historische Ticker-Identitaeten, echte Datenvintages und gepruefte Kapitalmassnahmen sind nicht vollstaendig vorhanden. Keine wissenschaftliche Validitaet oder profitabel-bewiesen-Aussage.", "",
        "Confluence-SETUP_READY hat im Gesamtsnapshot gegenueber Breakout-SETUP_READY einen hoeheren Mean, Median und eine hoehere Win Rate, aber weniger Beobachtungen. Gegenueber Minervini allein sind Median und Win Rate niedriger. Die unterschiedlichen Signalzeiten und die genannten Datenrisiken erlauben keine erzwungene kausale Nutzen- oder Profitabilitaetsaussage.", "",
        "## INTRADAY-TRIGGER-BACKTEST bleibt separat", "",
        "Im gespeicherten Pilot liegen ausreichende 5m-Daten fuer %d Aktien mit je 60 vollstaendigen Sitzungen vom %s bis %s vor. Fuer %d weitere Scanner-Ticker wurden im Daily-Ausbau keine Intraday-Daten angefragt. Sie sind nicht als vom Provider abgelehnt oder generell unavailable klassifiziert. Siehe intraday-coverage.csv." % (
            intra["adequate_stocks"], intra["first"], intra["last"], intra["not_collected_stocks"]), "",
        "Der alte Pilot meldete fuer Breakout TRIGGERED, EP, Parabolic Short/Long und Confluence TRIGGERED jeweils null bestaetigte Trigger-Beobachtungen. Diese Aussage gilt nur fuer die damaligen acht Aktien und deren damalige Rangpopulation. EP ist mangels historischem Catalyst-Archiv nicht vollstaendig evaluierbar. Aus fehlenden Beobachtungen folgt keine Abwesenheit historischer Gelegenheiten.", "",
        "Die gespeicherte Yahoo-Antwort begrenzt die gepruefte 5m-Anfrage auf 'last 60 days'. Diese Providergrenze beschraenkt den Trigger-Test und verwirft keine Daily-Beobachtung. Der erweiterte Daily-Bericht enthaelt keine aus Tageskerzen erfundenen OR-/EP-/Parabolic-Trigger und keine Intraday-Performancebehauptung.", "",
        "## Reproduktion", "",
        "Siehe [Daily-Anleitung](../../../DAILY.md). Raw-Dateien, feste Konfiguration, Kalender, Ticker-Ausschluesse, Signal-Ledger, Matrizen und 1.000 Zufallslaeufe sind gespeichert. verification.json enthaelt Eingabe-, Matrix-, Strategie- und Backtest-Hashes. Bestehende Strategiedateien stimmen mit origin/main %s ueberein. Keine Orders, kein Broker, kein Commit und kein Push." % expected["origin_main"], ""]
    (output / "REPORT.md").write_text("\n".join(lines))
    print("Wrote", output / "REPORT.md", flush=True)
    return combined


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--analysis", required=True)
    p.add_argument("--data", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--first", required=True)
    p.add_argument("--classification", required=True)
    a = p.parse_args()
    report(a.analysis, a.data, a.output, a.first, a.classification)
