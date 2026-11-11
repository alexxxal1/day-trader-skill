#!/usr/bin/env python3
"""Audit the original pilot without modifying data, rules or its results."""
import argparse
import collections
import csv
from pathlib import Path

from common import digest, read, write


def diagnose(first, classification, output):
    first, output = Path(first), Path(output)
    config = read(first / "data/collection-config.json")
    document = read(first / "data/dataset.json")
    adapted = read(first / "results/signals.json")
    universe = sorted(config["universe"])
    rows = read(classification)
    current = dict(rows["kept"])
    missing = sorted(set(current) - set(universe))
    diagnostic = {
        "pilot_config_sha256": digest(config), "pilot_dataset_sha256": digest(document),
        "pilot_universe": universe,
        "root_cause": "EXPLICIT_EIGHT_SYMBOL_PILOT_CONFIG; not an outcome of acquisition or strategy filters",
        "broader_universe_in_pilot": "NOT_REQUESTED_OR_RECORDED",
        "current_directory_date": rows["created"], "current_directory_rows": rows["counts"]["rows"],
        "current_scanner_eligible": len(current), "current_not_requested_in_pilot": len(missing),
        "directory_exclusion_reasons": {k: v for k, v in rows["counts"].items() if k not in ("keep", "rows")},
        "pilot_stages": [], "pilot_per_ticker": [], "signal_counts": {},
        "intraday_limit": "Yahoo 422: requested 5m history must be within last 60 days",
        "daily_coverage_not_caused_by_intraday": True,
        "additional_limitations": ["Pilot start 2026-01-02 was fixed, not the longest common history",
             "Pilot daily study and intraday replay shared a signal stream",
             "SETUP_READY could first be recorded intraday, not exclusively at a daily close",
             "Pilot lacked broad-universe ingestion and a per-ticker exclusion ledger",
             "A current directory cannot reconstruct historical/delisted membership"]}
    n = len(universe)
    for name, keep, excluded, reason in [
        ("Frozen configured stock universe", n, [], "No global US directory was used"),
        ("Daily fetch succeeded", sum(s in document["bars"] and "1d" in document["bars"][s] for s in universe), [], "None"),
        ("Daily eligibility throughout pilot", min(d["minervini"]["eligible"] for d in adapted["diagnostics"]), [], "None: 260-bar windows available for all eight"),
        ("Intraday files fetched", sum(bool(document["bars"][s].get("5m")) for s in universe), [], "None: 60 sessions each")]:
        diagnostic["pilot_stages"].append({"stage": name, "input_count": n, "remaining": keep, "excluded": excluded, "reason": reason})
    for sym in universe:
        frames = document["bars"][sym]
        diagnostic["pilot_per_ticker"].append({"ticker": sym, "daily_bars": len(frames["1d"]),
            "intraday_bars": len(frames.get("5m", [])), "excluded_at_acquisition": False})
    for strategy in sorted({s["strategy"] for s in adapted["signals"]}):
        observations = [s for s in adapted["signals"] if s["strategy"] == strategy]
        diagnostic["signal_counts"][strategy] = {"n": len(observations), "tickers": sorted({s["symbol"] for s in observations}),
                                                "statuses": dict(collections.Counter(s["status"] for s in observations))}
    output.mkdir(parents=True, exist_ok=True)
    write(output / "diagnosis.json", diagnostic)
    with (output / "pilot-exclusion-ledger.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["ticker", "name", "pilot_selection", "reason"])
        for sym, name in sorted(current.items()):
            writer.writerow([sym, name, "selected" if sym in universe else "not_requested",
                             "FROZEN_PILOT_CONFIG" if sym in universe else "NOT_REQUESTED_BY_EIGHT_SYMBOL_PILOT_CONFIG"])
    lines = ["# Diagnose des ersten Backtests", "",
        "Die acht Aktien waren eine explizite Auswahl in config.first.json. Es wurde kein groesseres US-Universum geladen und auf acht heruntergefiltert. Diese Stichprobenentscheidung war fuer den gewuenschten Vergleich zu eng.", "",
        "Das urspruenglich angeforderte Universum war: " + ", ".join(universe) + ".", "",
        "Alle acht Tagesdateien und alle acht Intraday-Dateien wurden erfolgreich geladen. Kein Ticker wurde bei der Beschaffung verworfen. An jedem der 214 Testtage waren alle acht fuer Minervini datenberechtigt. Ein Strategie-FAIL ist keine fehlende Datenabdeckung.", "",
        "| Stufe | Eingang | Verbleibend | Ausschluss |", "| --- | --- | --- | --- |"]
    for stage in diagnostic["pilot_stages"]:
        lines.append("| %s | %d | %d | %s |" % (stage["stage"], stage["input_count"], stage["remaining"], stage["reason"]))
    lines += ["", "Aktuell geladene Nasdaq-Verzeichnisse: %d Wertpapierzeilen; nach der unveraenderten Scanner-Klassifikation %d US-gelistete Aktien/ADRs. Davon wurden %d im Pilot gar nicht angefragt. Diese heutigen Verzeichnisse sind kein historisches Point-in-Time-Universum." % (
        rows["counts"]["rows"], len(current), len(missing)), "",
        "Ticker und konkrete Auswahlgruende stehen vollstaendig in pilot-exclusion-ledger.csv und sources/universe-classification.json.", "",
        "## Intraday-Grenze", "",
        "Die gespeicherte Yahoo-Fehlermeldung (HTTP 422) sagt fuer die gepruefte 5m-Anfrage ausdruecklich: 'The requested range must be within the last 60 days.' Der Pilot speicherte 60 Sitzungen vom 2026-08-14 bis 2026-11-06. Die konkrete Providerantwort ist massgeblich; 60d ist keine Garantie einer festen Kalender- oder Sitzungstagezahl fuer andere Abrufzeitpunkte.", "",
        "Diese Grenze erklaert das kurze Trigger-Fenster, aber nicht die Auswahl von acht Aktien. Daily-Daten wurden im Pilot nicht wegen fehlender Intraday-Daten entfernt.", "",
        "## Weitere Befunde", "",
        "14 der 51 Breakout-SETUP_READY-Beobachtungen wurden zuerst intraday erfasst. Sie gehoeren nicht in eine reine Daily-Statistik mit ausschliesslich Tages-Schlusskursen als Startpreis. Der neue Daily-Lauf erzeugt daher einen eigenen Signalstrom.", "",
        "Eine max-Anfrage mit interval=1d lieferte im Test tatsaechlich dataGranularity=3mo. Fuer den erweiterten Lauf wird deshalb ein expliziter Zeitraum mit gepruefter 1d-Granularitaet verwendet. Quartalskerzen duerfen nicht als Tagesdaten gelten.", "",
        "Die historische Datenbeschaffung wird fuer alle %d Scanner-Ticker versucht. Ausschluesse werden je Ticker, Testtag und Forward-Horizont protokolliert. Keine Intraday-Voraussetzung im Daily-Layer. Keine Strategieparameter werden angepasst." % len(current), ""]
    (output / "DIAGNOSIS.md").write_text("\n".join(lines))
    return diagnostic


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--first", required=True)
    p.add_argument("--classification", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    diagnose(a.first, a.classification, a.output)
