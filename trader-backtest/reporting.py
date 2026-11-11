"""Human-readable audit report. Complete metrics and observations stay in JSON."""
from adapters import STRATEGIES


def fmt(value, digits=2, percent=False):
    if value is None:
        return "n/a"
    return ("%." + str(digits) + "f") % (value * 100 if percent else value)


def table(headers, rows):
    return ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"] + [
        "| " + " | ".join(str(v) for v in row) + " |" for row in rows]


def render(report):
    audit = report["audit"]
    config = report["configuration"]
    overview = []
    for strategy in ("MINERVINI_FILTER", "MINERVINI_ENTRY", "QULLAMAGGIE_BREAKOUT", "CONFLUENCE_SETUP_READY", "CONFLUENCE_TRIGGERED"):
        f = report["signal_results"]["full"][strategy]["forward"]["20"]
        t = report["trade_results"].get("full", {}).get("REALISTIC/fixed_risk", {}).get(strategy, {})
        overview.append([strategy, report["strategy_counts"][strategy]["signals"], f["n"], fmt(f["mean"]),
                         fmt(f["win_rate"], percent=True), t.get("trades", {}).get("n", 0),
                         fmt(t.get("portfolio", {}).get("return_pct")),
                         ", ".join(f["flags"]) or "explorativ"])
    lines = ["# Reproduzierbarer erster Backtest", "",
             "Explorative historische Messung mit eingefrorenen Regeln. Keine Optimierung, keine Orders, kein Broker.", "",
             "## Wichtigste Vergleichswerte", "",
             "Forward Returns: gesamter Tages-Testzeitraum; Portfolio: gemeinsames Intraday-Fenster. REALISTIC, %.2f %% Risiko pro Trade, Hebel %.2f. n/a bezeichnet fehlende ausfuehrbare Trades bzw. fehlende Samples. Die Zeitraeume sind innerhalb jeder Auswertung fuer alle Strategien identisch." % (config["risk_per_trade"] * 100, config["leverage"]), ""]
    lines += table(["Variante", "Signale", "n 20d", "Mean 20d %", "Win 20d %", "Trades", "Portfolio %", "Hinweis"], overview)
    lines += ["",
             "## Daten und Geltungsbereich", "",
             "- Tagesstudie: %s bis %s, %d Sitzungen, %d Aktien: %s." % (
                 *audit["period"], audit["sessions"], audit["stock_count"], ", ".join(audit["universe"])),
             "- Kennzeichnungen: " + ", ".join(audit["flags"]),
             "- Keine delisteten Aktien und keine historische Universumsmitgliedschaft. Keine wissenschaftliche Validitaet.",
             "- Tageskurse sind ein heute gespeicherter, splitbereinigter Provider-Snapshot. Spaetere Revisionen sind nicht rekonstruierbar; angenommene Verfuegbarkeit am Kerzenschluss ist kein historischer Datenvintage-Nachweis.",
             "- Kurse ohne Dividenden; SPY/QQQ ebenfalls als Preisrendite. Keine erfundenen Borrow- oder CFD-Finanzierungskosten.",
             "- News-/Social-/Disclosure-Archiv fehlt. EP-Signale werden deshalb nicht aus heutigen Meldungen rekonstruiert.", ""]
    lines += ["Ohne historische Market-Cap-Metadaten nutzt Parabolic den vorhandenen UNKNOWN_OR_MID-Zweig. Aktuelle Marktkapitalisierungen werden nicht auf fruehere Trigger uebertragen. Das begrenzt auch die Interpretation ausbleibender Parabolic-Signale.", ""]
    lines += table(["Instrument", "Tageskerzen", "Historie", "Fehlende Testsitzungen", "Intraday-Sitzungen", "Intraday-Zeitraum"], [
        [s, c["daily_bars"], str(c["first"]) + " bis " + str(c["last"]), len(c["missing_sessions"]), c["intraday_sessions"],
         str(c["intraday_first"]) + " bis " + str(c["intraday_last"])] for s, c in audit["coverage"].items()])
    lines += ["", "Ungueltige OHLCV-Zeilen: %d. Abruffehler: %d." % (
        len(audit["invalid_rows"]), len(audit["provenance"].get("errors", []))), "",
        "**Vorab fixierte Annahmen:** Seed %d; %d Randomisierungen je verfuegbarer Kontrolle; Startkapital %.0f USD; maximal %d Positionen; Portfoliorisiko %.2f %%; Risiko pro Trade %.2f %%; Hebel %.2f." % (
            config["seed"], config["randomizations"], config["initial_equity"], config["max_positions"],
            config["max_portfolio_risk"] * 100, config["risk_per_trade"] * 100, config["leverage"]),
        "REALISTIC: voller Spread %.1f bp, Slippage %.1f bp pro Seite, %.3f USD Provision pro Aktie/Roundtrip. STRESS: %.1ffacher Spread, doppelte Slippage; IDEAL: jeweils Null. Es sind Annahmen, keine historischen Quote-/Gebuehrendaten." % (
            config["costs"]["spread_bps"], config["costs"]["slippage_bps_per_side"],
            config["costs"]["commission_per_unit_roundtrip"], config["costs"]["stress_spread_multiplier"]), "",
        "## Signalqualitaet: Forward Returns", "",
        "Brutto-Preisrenditen ab bekanntem Signalpreis bis zum Schlusskurs nach N weiteren Handelstagen. Ein Sample pro Ticker/Tag/Status; diese Beobachtungen ueberlappen. Median/Mean sind Prozentwerte, Win-/Verlustquote Anteile. Fehlende oder noch nicht abgeschlossene Horizonte werden gezaehlt, nicht mit Null aufgefuellt.", ""]
    for label, results in report["signal_results"].items():
        days = report["signal_periods"][label]
        lines += ["### " + label + " (" + days[0] + " bis " + days[-1] + ")", ""]
        for horizon in (1, 3, 5, 10, 20, 60):
            lines += ["**%d Handelstage**" % horizon, ""]
            rows = []
            for strategy in STRATEGIES:
                r = results[strategy]["forward"][str(horizon)]
                rows.append([strategy, r["n"], r["censored_or_missing"], fmt(r["mean"]), fmt(r["median"]),
                    fmt(r["win_rate"], percent=True), fmt(r["loss_rate"], percent=True), fmt(r["stdev"]),
                    fmt(r["max_positive_move_pct"]), fmt(r["max_negative_move_pct"]), fmt(r["excess_return_pct"]),
                    fmt(r["win_rate_vs_spy"], percent=True), ", ".join(r["flags"]) or "explorativ"])
            lines += table(["Strategie", "n", "zensiert/fehlend", "Mean %", "Median %", "Win %", "Loss %", "Std %", "MFE %", "MAE %", "Excess SPY %", "Win vs SPY %", "Flags"], rows) + [""]
    lines += ["Konfidenzintervalle in report.json: 95%-Bootstrap der Mittelwerte mit Bloecken von N Signaltagen. Bei weniger als zwei Bloecken kein Intervall. Auch diese Intervalle koennen die Abhaengigkeit und Universumsauswahl nicht beseitigen.", "",
              "## Trades und Portfolio", "",
              "Der Trade-Test umfasst nur die gemeinsame historische Intraday-Abdeckung aller acht Aktien. Die 70/30-Grenze fuer Trades wird innerhalb dieses kuerzeren Zeitraums gesetzt. Deshalb sind die Trade- und die Signal-IS/OOS-Daten unterschiedlich. Daten vor diesem Zeitraum werden nicht als signalfreie Cash-Performance ausgegeben.", "",
              "Filter und CANDIDATE haben keine Trade-Performance. SETUP_READY wird erst mit einem aktuellen bestaetigten Trigger und gueltigem Stop zum Trade. Der Vergleich nutzt den vorhandenen Exit-Planer mit vorab festgelegtem 2R-Ziel und 15:55-Glattstellung. Das ist ein einheitlicher Test der Entries unter dieser Exit-Regel.", ""]
    for label, scenarios in report["trade_results"].items():
        days = report["trade_periods"].get(label, [])
        if not days:
            lines += [label + ": keine gemeinsame Intraday-Historie.", ""]
            continue
        lines += ["### " + label + " (" + days[0] + " bis " + days[-1] + ")", ""]
        for scenario, results in scenarios.items():
            lines += ["**" + scenario + "**", ""]
            rows = []
            for strategy in STRATEGIES:
                t, p = results[strategy]["trades"], results[strategy]["portfolio"]
                rows.append([strategy, t["n"], fmt(t["win_rate"], percent=True), fmt(t["average_winner_usd"]), fmt(t["average_loser_usd"]),
                    fmt(t["expectancy_usd"]), fmt(t["profit_factor"]), fmt(t["average_r"]), fmt(t["median_r"]),
                    fmt(p.get("return_pct")), fmt(p.get("max_drawdown"), percent=True), fmt(p.get("exposure"), percent=True),
                    ", ".join(t["flags"] + p.get("flags", [])) or "explorativ"])
            lines += table(["Strategie", "Trades", "Win %", "Avg Win $", "Avg Loss $", "Expectancy $", "PF", "Avg R", "Median R", "Portfolio %", "DD %", "Exposure %", "Flags"], rows) + [""]
    lines += ["Durchschnitt/Median der Trade-Renditen, Haltedauer, Verlustserien, Sharpe/Sortino (nur ab 60 Tagen und positiver Varianz), Volatilitaet und einzelne R-Multiples stehen in report.json bzw. simulations/. Drawdown basiert auf taeglichem Mark-to-Market einschliesslich Startkapital; kein Tick-Drawdown.", "",
              "## Random Baseline", "",
              "Signal-Kontrolle: gleiche Anzahl Beobachtungen, Signalzeiten, Horizonte, Seiten und Aktienuniversum. Portfolio-Kontrolle: gleiche akzeptierte Trade-Anzahl, Entry-Zeiten und realisierte Haltedauern; zufaellige Aktien, identische Kosten/Sizing/Slots/Risikolimits, keine Ticker-Duplikate. Stopabstand wird fuer das Sizing relativ uebertragen, der Exit erfolgt zur gematchten Zeit. Dies ist eine bedingte Kontrolle der Aktienauswahl, kein unabhaengiger Strategietest. Ungueltige Ziehungen werden ausgewiesen.", ""]
    rows = []
    for strategy in STRATEGIES:
        r = report["signal_results"]["full"][strategy]["random_signal"]["20"]
        rows.append([strategy, "20d Signal-Mean %", r["valid"], r["invalid"], fmt(r["median"]), fmt(r["p05"]), fmt(r["p95"]), fmt(r["fraction_better_than_strategy"], percent=True)])
        tr = report["trade_results"].get("full", {}).get("REALISTIC/fixed_risk", {}).get(strategy, {}).get("random_portfolio")
        if tr:
            rows.append([strategy, "Portfolio %", tr["valid"], tr["invalid"], fmt(tr["median"]), fmt(tr["p05"]), fmt(tr["p95"]), fmt(tr["fraction_better_than_strategy"], percent=True)])
    lines += table(["Strategie", "Kontrolle", "gueltige Laeufe", "ungueltig", "Median", "P05", "P95", "Zufall besser %"], rows) + [""]
    lines += ["## SPY / QQQ Buy & Hold", ""]
    for label, scenarios in report["benchmarks"].items():
        rows = []
        for cost, benchmarks in scenarios.items():
            for sym, b in benchmarks.items():
                rows.append([sym, cost, fmt(b.get("return_pct")), fmt(b.get("max_drawdown"), percent=True),
                             fmt(b.get("annualized_volatility"), percent=True), fmt(b.get("exposure"), percent=True)])
        lines += ["**" + label + " (Trade-Zeitraum)**", ""] + table(["ETF", "Kosten", "Rendite %", "DD %", "Volatilitaet % p.a.", "Exposure %"], rows) + [""]
    lines += ["Buy & Hold von der ersten Eroeffnung zum letzten Schluss; Preisrendite ohne Dividenden. Benchmarks fuer die laengeren Tagesperioden separat in report.json unter daily_benchmarks.", "",
              "## Confluence-Frage", "",
              "Direkter Vergleich bei REALISTIC / fixed_risk; Signalqualitaet bei 20 Tagen. CANDIDATE, SETUP_READY und TRIGGERED werden separat ausgewiesen. Unterschiede in Zeitpunkt und Stichprobengroesse sind Teil der Auswahlwirkung, kein Beweis eines kausalen Nutzens.", ""]
    rows = []
    for strategy in ("MINERVINI_FILTER", "MINERVINI_ENTRY", "QULLAMAGGIE_BREAKOUT", "CONFLUENCE_CANDIDATE", "CONFLUENCE_SETUP_READY", "CONFLUENCE_TRIGGERED"):
        f = report["signal_results"]["full"][strategy]["forward"]["20"]
        t = report["trade_results"].get("full", {}).get("REALISTIC/fixed_risk", {}).get(strategy, {})
        p = t.get("portfolio", {})
        t = t.get("trades", {})
        rows.append([strategy, report["strategy_counts"][strategy]["signals"], f["n"], fmt(f["mean"]), f["loss_count"], fmt(f["loss_rate"], percent=True),
                     t.get("n", 0), fmt(t.get("win_rate"), percent=True), fmt(t.get("expectancy_usd")), fmt(p.get("max_drawdown"), percent=True)])
    lines += table(["Variante", "Signale", "n Forward", "Mean 20d %", "schlechte Signale n", "schlechte Signale %", "Trades", "Trade Win %", "Expectancy $", "DD %"], rows) + ["",
              "Weniger Signale allein reichen nicht fuer eine Qualitaetsverbesserung. Ergebnisse mit LOW_SAMPLE_CONFIDENCE, ohne Trades oder ohne vergleichbare Zeitabdeckung erlauben keine belastbare Rangfolge. Keine Schlussfolgerung wird erzwungen.", "",
              "## Walk Forward und methodische Grenzen", "",
              "Signal-Walk-Forward-Fenster: %d. Trade-Walk-Forward-Fenster: %d." % (len(report["walk_forward"]), len(report["trade_walk_forward"])),
              "Ein Trade-Walk-Forward mit 80 Trainings- und 40 Testsitzungen benoetigt mindestens 120 gemeinsame Intraday-Sitzungen. Bei kuerzerer Historie: INSUFFICIENT_HISTORY_FOR_WALK_FORWARD. Die Signal-Fenster verwenden unveraenderte Parameter, die Trainingsperiode dient keiner Anpassung.", "",
              "Belastbar sind die getesteten Rechenregeln und die Reproduzierbarkeit auf dem gespeicherten Snapshot. Strategierenditen bleiben wegen Universumsauswahl, Revisionen, kurzem Intraday-Fenster, fehlenden historischen Borrow-/CFD-Daten und teilweise kleinen Stichproben explorativ. Keine profitabel-bewiesen-Aussage.", "",
              "## Reproduktion", "",
              "Siehe [SKILL.md](../../../SKILL.md) und die gespeicherten frozen-config.json, data-audit.json, signals.json, forward-observations.json, report.json sowie simulations/. Eingabe-, Engine- und Strategie-Hashes sind im JSON-Bericht festgehalten. Ein erneuter Offline-Lauf mit denselben Dateien und Python-Version erzeugt identische numerische Ergebnisse.", ""]
    return "\n".join(lines)
