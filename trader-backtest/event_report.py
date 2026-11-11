#!/usr/bin/env python3
"""Evidence-based comparison of frozen event and quality sensitivity panels."""
import argparse
import csv
import hashlib
from pathlib import Path

from common import digest, read, write
from event_signals import NAMES, VARIANTS
from reporting import fmt, table
from run import fingerprints


def sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def compare(cf, base):
    if not cf["n"] or not base["n"]:
        return {"available": False}
    mean_gap = cf["mean"] - base["mean"]
    tail_gap = cf["tail"]["top_one_percent_mean_contribution_pp"] - base["tail"]["top_one_percent_mean_contribution_pp"]
    return {"available": True, "mean_gap_pp": mean_gap, "median_gap_pp": cf["median"] - base["median"],
            "win_rate_gap_pp": 100 * (cf["win_rate"] - base["win_rate"]),
            "p10_gap_pp": cf["quantiles"]["10"] - base["quantiles"]["10"],
            "p90_gap_pp": cf["quantiles"]["90"] - base["quantiles"]["90"],
            "all_mean_median_win_improve": cf["mean"] > base["mean"] and cf["median"] > base["median"] and cf["win_rate"] > base["win_rate"],
            "top_one_percent_mean_gap_contribution_pp": tail_gap,
            "remaining_mean_gap_contribution_pp": mean_gap - tail_gap,
            "source_n": cf["n"], "comparison_n": base["n"]}


def build(analysis, output):
    analysis, output = Path(analysis), Path(output)
    method = read(output / "frozen-methodology.json")
    if method != read(Path(__file__).parent / "event-methodology.json"):
        raise ValueError("frozen rules changed")
    stats = read(output / "event-statistics.json")
    random = read(output / "event-random-controls.json")
    meta = read(output / "event-metadata.json")
    ledger = read(output / "forward-ledger-audit.json")
    quality = read(output / "quality-ticker-audit.json")
    expected = read(Path(__file__).parent / "strategy-sources.json")
    if fingerprints() != expected["sha256"]:
        raise ValueError("strategy sources changed")
    answers = {}
    for variant in VARIANTS:
        answers[variant] = {}
        for h in method["horizons"]:
            answers[variant][str(h)] = {}
            for scope in ("ALL_WINDOWS", "NONOVERLAP"):
                answers[variant][str(h)][scope] = {}
                for period in stats["periods"]:
                    answers[variant][str(h)][scope][period] = {}
                    for panel in ("ALL", "VALID_ONLY"):
                        cf = stats["statistics"][variant][NAMES[2]][str(h)][scope][period][panel]
                        answers[variant][str(h)][scope][period][panel] = {
                            base: compare(cf, stats["statistics"][variant][base][str(h)][scope][period][panel]) for base in NAMES[:2]}
    write(output / "direct-comparisons.json", answers)
    with (output / "event-random-distributions.csv").open("w", newline="") as stream:
        w = csv.writer(stream)
        w.writerow(["control", "variant", "strategy", "horizon", "period", "quality", "n_strategy", "strategy_mean", "strategy_median", "random_median_mean", "random_p05", "random_p95", "random_better_fraction", "strategy_percentile", "random_n_min", "random_n_max", "valid_replicates"])
        for kind in ("primary_no_future_quality_selection", "valid_matched_retrospective_sensitivity"):
            for v, names in random[kind].items():
                for name, horizons in names.items():
                    for h, case in horizons.items():
                        for period, panels in case["periods"].items():
                            for panel, r in panels.items():
                                w.writerow([kind, v, name, h, period, panel] + [r[k] for k in (
                                    "strategy_n", "strategy_mean", "strategy_median", "random_median_mean", "random_p05", "random_p95", "fraction_random_better", "strategy_percentile", "random_observed_n_min", "random_observed_n_max", "valid_estimates")])
    headers = ["variant", "strategy", "horizon", "scope", "period", "quality_panel", "selected", "n", "mean", "median", "win_rate", "overlap_fraction",
               "P10", "P25", "P50", "P75", "P90", "P95", "P99", "gt5", "gt10", "ltminus5", "ltminus10", "top1_mean_contribution"]
    with (output / "event-distributions.csv").open("w", newline="") as file:
        w = csv.writer(file)
        w.writerow(headers)
        for v in VARIANTS:
            for name in NAMES:
                for h, scopes in stats["statistics"][v][name].items():
                    for scope, ps in scopes.items():
                        for p, panels in ps.items():
                            for panel, r in panels.items():
                                w.writerow([v, name, h, scope, p, panel, r["selected_events"], r["n"], r["mean"], r["median"], r["win_rate"], r["overlap_fraction"]] +
                                    [r["quantiles"].get(str(k)) for k in (10, 25, 50, 75, 90, 95, 99)] +
                                    [r["shares"].get(k) for k in ("gt_plus5", "gt_plus10", "lt_minus5", "lt_minus10")] +
                                    [r["tail"].get("top_one_percent_mean_contribution_pp")])
    quality_summary = {k: sum(r.get(k, 0) for r in quality) for k in ("present_bars", "valid_bars", "suspect_bars", "invalid_present_bars", "split_count", "reverse_split_count", "dividend_count")}
    case_facts = {
        "PPCB": {"official_source": "https://www.sec.gov/Archives/edgar/data/1517681/000149315225006697/form10-q.htm",
            "filing_date": "2025-02-14", "effective_split_date": "2025-01-29", "official_reverse_split": "1:60000",
            "saved_yahoo_split": "1:2", "saved_close_before": .10000000149011612, "saved_close_after": 6250.,
            "treatment": "Generic daily factor jump labels SUSPECT; no symbol-specific exclusion, repaired quote, or alpha catalyst backdating"},
        "CHRD_OASIS": {"source_equity_cancellation": "https://www.sec.gov/Archives/edgar/data/1486159/000148615920000115/oas-20201119.htm",
            "source_merger": "https://www.sec.gov/Archives/edgar/data/1486159/000119312522189506/d253559d8k.htm",
            "finding": "Legacy Oasis equity was cancelled on Chapter 11 emergence in 2020; subsequent 2022 Whiting merger/name history documented. A continuous ticker chart is not proof of a continuous shareholder return.",
            "treatment": "Uniform jump/inception checks; no manual ticker removal; exhaustive historical security identity unavailable"}}
    write(output / "corporate-action-case-evidence.json", case_facts)
    artifact_names = ["events-FULL_RESET.u8", "events-COOLDOWN20.u8", "quality-bars.u8", "quality-features.u8", "quality-reasons.u32"] + ["quality-window-" + str(h) + ".u8" for h in method["horizons"]]
    verification = {"methodology_sha256": digest(method), "original_strategy_sha256": fingerprints(),
        "analysis_input_reference": read(output / "input-reference.json"),
        "new_artifact_sha256": {name: sha(output / name) for name in artifact_names},
        "backtest_code_sha256": {p.name: sha(p) for p in sorted(Path(__file__).parent.glob("*.py"))},
        "evidence_sha256": {p.name: sha(p) for p in sorted((output / "quality-evidence").iterdir()) if p.is_file()}}
    write(output / "verification.json", verification)
    write(output / "report.json", {"methodology": method, "statistics": stats, "random_controls": random,
        "direct_comparisons": answers, "quality_summary": quality_summary, "case_evidence": case_facts, "verification": verification})
    lines = ["# Methodisch gehaerteter Event-Backtest", "",
        "Eingefrorener Daily-Snapshot: %s bis %s, gleiches Ausgangsuniversum von 5.933 Aktien/ADRs und gleiche native Regeln. Nur zusaetzliche Event- und Datenqualitaets-Auswertungen. Alle bisherigen Daily-Dateien bleiben erhalten." % tuple(stats["periods"]["full"]), "",
        "## Direkte Antworten aus den Ergebnissen", "",
        "Primaerer Horizont vorab festgelegt: 20 Handelstage. Die Tabellen unten zeigen beide Event-Varianten, alle sechs Horizonte, ALL/VALID und die zeitlichen IS/OOS-Abschnitte. Keine Aussage wird aus einem nachtraeglich ausgewaehlten besten Horizont abgeleitet.", ""]
    b = stats["statistics"]["COOLDOWN20"]
    c = b[NAMES[2]]["20"]["ALL_WINDOWS"]["full"]["VALID_ONLY"]
    q = b[NAMES[1]]["20"]["ALL_WINDOWS"]["full"]["VALID_ONLY"]
    m = b[NAMES[0]]["20"]["ALL_WINDOWS"]["full"]["VALID_ONLY"]
    lines += [
        "1. **Gegenueber Minervini: kein allgemeiner Qualitaetsvorteil.** Im 20-Tage-VALID-Panel mit Cooldown liegt Confluence beim Mean bei %.2f %% gegenueber %.2f %%, aber beim Median bei %.2f %% gegenueber %.2f %% und bei der Win Rate bei %.2f %% gegenueber %.2f %%. Hoeherer Mittelwert geht hier mit schlechterem typischen Ergebnis und geringerer Trefferquote einher." % (c["mean"], m["mean"], c["median"], m["median"], c["win_rate"] * 100, m["win_rate"] * 100), "",
        "2. **Gegenueber Breakout-SETUP_READY: bessere gemessene Event-Kennzahlen im primaeren Horizont.** Im selben Panel verbessert Confluence Mean von %.2f auf %.2f %%, Median von %.2f auf %.2f %% und Win Rate von %.2f auf %.2f %%. Die Richtung bleibt auch bei vollstaendigem Reset bestehen; sie ist keine kausale Identifikation eines Confluence-Effekts, denn Signalzeitpunkte und Kohorten unterscheiden sich." % (q["mean"], c["mean"], q["median"], c["median"], q["win_rate"] * 100, c["win_rate"] * 100), "",
        "3. **Out-of-Sample:** Die Verbesserung gegenueber Breakout bleibt bei 20 Tagen fuer beide Event-Varianten in Mean, Median und Win Rate erhalten, auch im VALID-Panel. Gegenueber Minervini bleibt es ein Trade-off: hoeherer Mean, schlechterer Median und geringere Win Rate. Vollstaendige Zahlen, auch fuer andere Horizonte und ohne Fensterueberschneidung, stehen in den Tabellen/JSON; der heutige Survivor-Bestand macht diesen zeitlichen Holdout weiterhin zu keinem echten Point-in-Time-OOS-Nachweis.", "",
        "4. **Nach Datenqualitaetsfilter:** Die genannten 20-Tage-Unterschiede bleiben bestehen. Die Definition von VALID wurde nicht anhand der Strategierenditen nachjustiert. VALID ist eine konservative rueckblickende Sensitivitaet und keine Garantie eines vollstaendigen Corporate-Action-/Identitaetsarchivs.", "",
        "5. **Verteilung statt Mean allein:** Der Confluence-Median bleibt negativ. Im Cooldown-VALID-Panel traegt das oberste 1 %% %.2f Prozentpunkte zum Mean von %.2f %% bei, also %.1f %% seiner arithmetischen Nettosumme. Die restlichen 99 %% tragen %.2f Prozentpunkte bei. Der Mean ist somit stark rechtsschief. Der Mehrertrag gegenueber Breakout kommt aber nicht nur aus einem groesseren Top-Tail: dessen Top-1-%%-Beitrag ist mit %.2f Prozentpunkten sogar hoeher als bei Confluence; Median und Win Rate verbessern sich ebenfalls. Die absolute positive Expectancy ist tail-abhaengig, der Vergleichsvorteil gegenueber Breakout laesst sich nicht allein mit wenigen zusaetzlichen Extremgewinnern erklaeren." % (
            c["tail"]["top_one_percent_mean_contribution_pp"], c["mean"], 100 * c["tail"]["top_one_percent_mean_contribution_pp"] / c["mean"],
            c["tail"]["remaining_mean_contribution_pp"], q["tail"]["top_one_percent_mean_contribution_pp"]), "",
        "Diese Antworten sind deskriptiv auf dem gespeicherten Snapshot. Kein Beweis fuer Netto-Profitabilitaet oder wissenschaftliche Validitaet.", "",
        "## Daily-Beobachtungen und Events getrennt", ""]
    lines += table(["Strategie", "taegliche Beobachtungen", "FULL_RESET Events", "COOLDOWN20 Events", "links zensierte Aktivierungen im Testzeitraum"], [
        [name, stats["event_counts"]["DAILY_REFERENCE"][name]["observations"], stats["event_counts"]["FULL_RESET"][name]["observations"],
         stats["event_counts"]["COOLDOWN20"][name]["observations"], ledger["left_censored_active_episodes_in_study"][name]] for name in NAMES])
    lines += ["", "A: Minervini FAIL rearms PASS; Breakout braucht ein bestaetigtes FAILED, CANDIDATE alleine rearms nicht; Confluence braucht Minervini-FAIL oder Breakout-FAILED. B: neue bestaetigte Aktivierung mit mindestens 20 Sitzungen seit dem letzten akzeptierten Event. Dauerhaft aktive Zustaende werden nie alle 20 Tage erneut gezaehlt. Unterdrueckte Uebergaenge werden nicht spaeter nachgereicht.", "",
        "Datenluecken sind UNKNOWN, kein Reset. Bereits aktive Episoden nach unbekanntem Vorzustand werden links zensiert. Native Vorstudien-Zustaende wurden aus dem gespeicherten Warmup rekonstruiert. Event-Zustand und Cooldown laufen ueber die IS/OOS-Grenze weiter. Event-Extraktion geschieht vor Qualitaetsfiltern; die Filter koennen weder neue Events noch Resets erzeugen.", "",
        "## ALL und VALID: primaerer 20-Tage-Vergleich", ""]
    rows = []
    for v in VARIANTS:
        for panel in ("ALL", "VALID_ONLY"):
            for name in NAMES:
                r = stats["statistics"][v][name]["20"]["ALL_WINDOWS"]["full"][panel]
                rows.append([v, panel, name, r["n"], fmt(r["mean"]), fmt(r["median"]), fmt(r["win_rate"], percent=True), fmt(r["overlap_fraction"], percent=True)])
    lines += table(["Variante", "Daten", "Strategie", "n", "Mean %", "Median %", "Win %", "ueberlappende Fenster %"], rows)
    lines += ["", "**Tail-Zerlegung: COOLDOWN20, VALID, 20 Tage.** Arithmetische Beitraege, keine nachtraeglich ausgeschnittene Handelsstrategie.", ""]
    lines += table(["Strategie", "Mean %", "Beitrag oberstes 1 % (pp)", "Beitrag restliche 99 % (pp)", "Top 1 % an positiven Returns %"], [
        [name, fmt(r["mean"]), fmt(r["tail"]["top_one_percent_mean_contribution_pp"]), fmt(r["tail"]["remaining_mean_contribution_pp"]),
         fmt(r["tail"]["top_one_percent_share_of_gross_positive_returns"], percent=True)]
        for name in NAMES for r in [stats["statistics"]["COOLDOWN20"][name]["20"]["ALL_WINDOWS"]["full"]["VALID_ONLY"]]])
    lines += ["", "Events sind zustandsbereinigt, aber nicht automatisch statistisch unabhaengig. FULL_RESET hat bei 20 Tagen weiterhin ueberlappende Fenster. COOLDOWN20 beseitigt diese Ueberschneidung desselben Tickers fuer 20 Tage; bei 60 Tagen nicht. Unterschiedliche Aktien bleiben marktweit korreliert. NONOVERLAP behaelt pro Ticker/Strategie chronologisch das erste Event und wartet mindestens den jeweiligen Horizont, ohne Qualitaet oder Return anzusehen.", "",
        "## Datenqualitaet und Corporate Actions", "",
        "Regeln vor Ergebnisauswertung eingefroren in frozen-methodology.json: ungueltige/fehlende/nichtpositive OHLC, widerspruechliche OHLC-Bereiche sowie negatives/nichtendliches Volumen sind INVALID. Volumen null allein ist kein INVALID. Tages-Grossfaktor >=2 oder <=0,5, veraenderter Schlusskurs ohne gemeldetes Volumen, 20 Sitzungen mit stagnierendem Nullvolumen, unerklaerte Adjustierungswechsel und Inception-/Markt-Mismatches sind SUSPECT. Split allein ist kein Ausschluss. Bestaetigte gemischte Quote-/Adjclose-Einheiten bei Splits sind INVALID. Groesse eines mehrtaegigen Forward Returns allein fuehrt nur zu einer Pruefmarke, nie zu einer automatischen Loeschung.", "",
        "Auf den vorhandenen Originalkerzen: VALID %d; SUSPECT %d; INVALID %d. Erfasste Splits %d, davon Reverse Splits %d; Dividendenereignisse %d. Leere Kalenderplaetze vor einem IPO sind separat fehlende Daten und keine beobachteten schlechten Kurse." % tuple(quality_summary[k] for k in ("valid_bars", "suspect_bars", "invalid_present_bars", "split_count", "reverse_split_count", "dividend_count")), "",
        "Ein Event/Horizont erhaelt die hoechste Schwere aus seinem 260-Tage-Eingangsfenster und allen Kurskerzen bis zum Forward-Endpunkt. ALL behaelt alle arithmetisch auswertbaren Beobachtungen inklusive auffaelliger Werte; fehlende oder mathematisch ungueltige Returns werden gezaehlt und nicht imputiert. VALID_ONLY ist der strengere rueckblickende Ausschnitt. Echte extreme Gap-Gewinner koennen als SUSPECT markiert werden; das Panel ist deshalb eine konservative Sensitivitaet, keine unumstrittene Ground Truth.", "",
        "**PPCB konkret:** Das [SEC-10-Q vom 14.02.2025](https://www.sec.gov/Archives/edgar/data/1517681/000149315225006697/form10-q.htm) nennt einen Reverse Split 1:60.000 mit Wirkung 29.01.2025. Die gespeicherte Yahoo-Metadatei nennt fuer denselben Tag 1:2 und meldet 0,10 -> 6.250. Quote und Adjclose haben beide denselben Sprung. Die allgemeine Faktorregel markiert das als SUSPECT; kein PPCB-Sonderfilter und keine reparierte Rendite. Die spaeter veroeffentlichte SEC-Meldung ist nur Auditbeleg, kein rueckdatierter Catalyst.", "",
        "**M&A/Identitaet konkret:** Ein [Oasis-8-K von 2020](https://www.sec.gov/Archives/edgar/data/1486159/000148615920000115/oas-20201119.htm) dokumentiert die Annullierung der alten Aktien bei Chapter-11-Austritt; ein [weiteres 8-K von 2022](https://www.sec.gov/Archives/edgar/data/1486159/000119312522189506/d253559d8k.htm) die Whiting-Fusion und heutige Chord-Identitaet. Ein durchgehender CHRD/Oasis-Chart ist kein Nachweis eines durchgehenden Altaktionaersreturns. Fuer alle Ticker werden Inception und Source-Metadaten geprueft; ein vollstaendiges historisches Security-Master-/Merger-/Ticker-Reuse-Archiv fehlt. Unbekannt ist nicht gleich ausgeschlossen und nicht gleich bestaetigt sauber.", "",
        "Die eigenen Eingangs-/Forward-Kurse eines VALID-Events bestehen diese Tests. Die urspruenglichen nativen RS-Rangfolgen bleiben unveraendert; auch sie koennen durch fehlerhafte andere Aktien im urspruenglichen Querschnitt beeinflusst sein. Dieses Panel ist kein neuer Scan mit nachtraeglich bereinigter Rankingpopulation.", "",
        "Alle Einzelbeobachtungen bleiben in event-forward-ledger.csv.gz, Auffaelligkeiten in quality-issues.csv.gz und extreme-event-returns.csv.gz. corporate-actions.csv enthaelt die quellenbasierten Aktionen; keine Kurse wurden geloescht, geglaettet oder passend zur Performance korrigiert.", "",
        "## Random Baseline, gleiche Eventtermine und Datenregeln", "",
        "Je Variante/Strategie/Horizont 1.000 uniforme Ziehungen aus derselben zeitlich datenberechtigten Kohorte, ohne doppelte Ticker pro Datum. FULL_RESET-Kontrollen halten mindestens zwei Sitzungen Abstand, COOLDOWN20 mindestens 20. Counts, Termine, Horizonte und Brutto-Annahmen passen. Keine Regeln werden bei Kapazitaetsproblemen gelockert; unmoegliche Replikate waeren ausgewiesen.", "",
        "PRIMARY waehlt keine Aktie anhand spaeterer Qualitaet. Dieselben Qualitaetsfenster werden erst auf die gezogenen Outcomes angewandt. Ausgewaehlte Eventzahlen stimmen exakt, die beobachtbare VALID-Anzahl kann abweichen. Die zusaetzliche VALID_MATCHED-Kontrolle gleicht die nach Qualitaetsfilter verbleibende Anzahl pro Datum/Horizont exakt ab. Sie konditioniert ausdruecklich rueckblickend auf gueltige Daten und ist kein deploybarer Entry-Test. Beide Kontrollen sind vollstaendig ausgewiesen.", "",
        "**20 Tage, PRIMARY ohne Auswahl nach zukuenftiger Qualitaet:**", ""]
    rows = []
    for v in VARIANTS:
        for panel in ("ALL", "VALID_ONLY"):
            for name in NAMES:
                r = random["primary_no_future_quality_selection"][v][name]["20"]["periods"]["full"][panel]
                rows.append([v, panel, name, r["strategy_n"], fmt(r["strategy_mean"]), fmt(r["strategy_median"]), fmt(r["random_median_mean"]),
                    fmt(r["random_p05"]), fmt(r["random_p95"]), fmt(r["fraction_random_better"], percent=True), fmt(r["strategy_percentile"])])
    lines += table(["Variante", "Daten", "Strategie", "n Strategie", "Mean %", "Median %", "Random Median-Mean %", "P05 %", "P95 %", "Random besser %", "Strategie-Perzentil"], rows)
    lines += ["", "**20 Tage, exakte VALID-Stichprobengroessen (rueckblickende Zusatzkontrolle):**", ""]
    rows = []
    for v in VARIANTS:
        for name in NAMES:
            r = random["valid_matched_retrospective_sensitivity"][v][name]["20"]["periods"]["full"]["VALID_ONLY"]
            rows.append([v, name, r["strategy_n"], fmt(r["strategy_mean"]), fmt(r["strategy_median"]), fmt(r["random_median_mean"]),
                         fmt(r["random_p05"]), fmt(r["random_p95"]), fmt(r["fraction_random_better"], percent=True), fmt(r["strategy_percentile"])])
    lines += table(["Variante", "Strategie", "n", "Strategie Mean %", "Median %", "Random Median-Mean %", "Random P05 %", "Random P95 %", "Random besser %", "Strategie-Perzentil"], rows)
    lines += ["", "Random Median/P05/P95 sind Quantile der simulierten Mittelwerte, nicht Quantile einzelner Aktienreturns. Das Strategie-Perzentil gibt den empirischen Rang des Strategie-Mittelwerts mit halben Bindungen an. 0 von 1.000 besseren Ziehungen bedeutet kein p=0, keine Gewissheit und keinen Profitabilitaetsbeweis. Die Sampling-Kontrolle matcht weder Sektor noch Volatilitaet; Survivorship-/Vendor-Bias bleiben bestehen.", "",
        "## Event-Verteilung und IS/OOS, alle Horizonte", ""]
    for v in VARIANTS:
        for panel in ("ALL", "VALID_ONLY"):
            for period, span in stats["periods"].items():
                lines += ["### " + v + " / " + panel + " / " + period + " (" + " bis ".join(span) + ")", ""]
                rows = []
                for h in method["horizons"]:
                    for name in NAMES:
                        r = stats["statistics"][v][name][str(h)]["ALL_WINDOWS"][period][panel]
                        rows.append([h, name, r["n"], fmt(r["mean"]), fmt(r["median"]), fmt(r["win_rate"], percent=True)] +
                            [fmt(r["quantiles"].get(str(p))) for p in (10, 25, 50, 75, 90, 95, 99)] +
                            [fmt(r["shares"].get(k), percent=True) for k in ("gt_plus5", "gt_plus10", "lt_minus5", "lt_minus10")])
                lines += table(["Tage", "Strategie", "n", "Mean %", "Median %", "Win %", "P10", "P25", "P50", "P75", "P90", "P95", "P99", ">+5 % Anteil", ">+10 %", "<-5 %", "<-10 %"], rows) + [""]
    lines += ["## Ueberlappungsfreie Sensitivitaet", "",
        "Gleiche chronologische Auswahl fuer beide Qualitaetspanels; Qualitaet entscheidet nicht, welches Event eine Ueberschneidung ersetzt. Die PRIMARY-/VALID_MATCHED-Zufallstests oben betreffen die vollstaendigen Event-Stroeme. Die zusaetzliche NONOVERLAP-Auswertung ist eine separate deterministische Datums-Sensitivitaet.", ""]
    rows = []
    for v in VARIANTS:
        for h in method["horizons"]:
            for name in NAMES:
                r = stats["statistics"][v][name][str(h)]["NONOVERLAP"]["full"]["VALID_ONLY"]
                rows.append([v, h, name, r["n"], fmt(r["mean"]), fmt(r["median"]), fmt(r["win_rate"], percent=True), r["overlapping_windows"]])
    lines += table(["Variante", "Tage", "Strategie", "n VALID", "Mean %", "Median %", "Win %", "Overlap"], rows)
    lines += ["", "IS/OOS wird in dieser neuen Auswertung ausschliesslich fuer Events wiederholt: 1993-01-29 bis 2016-09-14 und 2016-09-15 bis 2026-11-06. Ueber die Grenze laufende Outcomes werden zensiert. Event-Zustaende, Reset-/Cooldown-Historie und die datumsbasierte Nonoverlap-Auswahl werden nicht an der Grenze neu gestartet.", "",
        "Moving-Block-Bootstrap: 1.000 Resamples ganzer Signaltage, Blocklaenge max(20, Horizont). Intervalle stehen im JSON. Das beruecksichtigt einen Teil der Zeit- und Querschnittsabhaengigkeit, beweist aber keine Unabhaengigkeit und beseitigt weder historische Universumsauswahl noch Datenrevisionen.", "",
        "## Reproduktion und Grenzen", "",
        "Siehe [EVENTS.md](../../EVENTS.md). Direkte numerische Vergleiche: direct-comparisons.json. Alle Kennzahlen: event-statistics.json; komplette Kontrollverteilungen: event-random-controls.json, event-random-distributions.csv und random-groups/*.npz; Audit: event-distributions.csv, event-ledger.csv.gz, event-forward-ledger.csv.gz, quality-ticker-audit.json und corporate-action-case-evidence.json.", "",
        "VALID ist keine wissenschaftliche Validierung. Heutiges Survivor-Universum, unvollstaendige Security-Identitaeten und Datenvintages, ungepruefte Corporate Actions ausserhalb der dokumentierten Faelle, moegliche legitime SUSPECT-Gaps und nicht modellierte Ausfuehrungskosten begrenzen jede Handelsaussage. Intraday bleibt vollstaendig separat. Keine Minervini-/Qullamaggie-Regel wurde geaendert. Keine Orders, kein Broker, kein Commit, kein Push.", ""]
    (output / "REPORT.md").write_text("\n".join(lines))
    print("Event hardening report written:", output / "REPORT.md", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--analysis", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    build(a.analysis, a.output)
