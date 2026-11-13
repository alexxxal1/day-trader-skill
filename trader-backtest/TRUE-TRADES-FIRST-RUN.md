# Erster tatsächlicher Intraday-Rekonstruktionslauf

Stand 13.11.2026. Basis nach `git fetch origin` und Fast-forward:
`origin/main` = `a9d6f68`. Workspace war vor der Erweiterung sauber.
Alle Strategieparameter und bestehenden Python-Strategiequellen sind unverändert;
ihre SHA256 stimmen weiterhin mit `strategy-sources.json` überein.
Keine Optimierung, Orders, Broker-Aufrufe, Commits oder Pushes.

Methodik, Datenquellen-Audit, Datensatzvertrag und Reproduktionsbefehle:
[TRUE-TRADES.md](TRUE-TRADES.md).

## Tatsächliche Datenabdeckung und Umfang

`INTRADAY_HISTORY_LIMITED`, `NOT_HISTORICALLY_PIT_CERTIFIED`.

Die zulässigen Yahoo-Antworten wurden zunächst separat auditiert, anschließend
der bereits vorab definierte Acht-Aktien-Pilot aus `config.first.json`
eingefroren und vollständig über seinen gelieferten Intraday-Zeitraum abgespielt.
Das frühere breite Daily-Universum und dessen Historie 1993–2026 sind **nicht**
der Umfang dieses ersten Intraday-Laufs. Kein Ticker wurde manuell entfernt.

* Verwendete Intraday-Sessions: **20.08.2026 bis 12.11.2026**, 60 Sessions.
* 5m-Antworten: ab 20.08.2026; 1m-Antworten: ab 04.11.2026, sieben Sessions.
* Acht Aktien mit jeweils mindestens einer vollständigen regulären Session.
* 480 Aktien-Sessions mit zusammenhängendem Opening-Prefix;
  **478 vollständige reguläre Aktien-Sessions**.
* Frame-Wahl: pro Aktie 53 Sessions mit 5m und sieben mit 1m.
  ORH/ORL-1m ist an den älteren 53 Sessions nicht rekonstruierbar.
* DJT hat am 06.11. und 10.11. jeweils eine fehlende 1m-Bar: 389 statt 390.
  Ein Prefix bleibt nutzbar; ein späterer unvollständiger Exit wäre gesperrt.
* Daily-Kontext: für alle acht Titel 501 abgeschlossene Bars im gelieferten
  `2y`-Snapshot, zur Entscheidung nur jeweils vergangene Sessions.

| Aktie | Intraday-Sessions | Vollständige Sessions |
|---|---:|---:|
| AAPL | 60 | 60 |
| AXP | 60 | 60 |
| BAC | 60 | 60 |
| DJT | 60 | 58 |
| GOOGL | 60 | 60 |
| KO | 60 | 60 |
| NVDA | 60 | 60 |
| TSLA | 60 | 60 |

Yahoo verweigert 5m/15m-`90d` mit „last 60 days“, 1m-`30d` mit
„Only 8 days ... per request“, und 60m-`3y` mit „last 730 days“.
Die akzeptierten `range`-Antworten und deren tatsächlich beobachtete
Kalenderzeiträume sind im Quellen-Audit ausdrücklich separat dokumentiert.
60m-Daten ab 15.12.2023 wurden nur auditiert und ersetzen keine fehlenden
1m-/5m-Trigger im bestehenden nativen Vertrag.

## Screening, Trigger und vollständige True Trades

Primär: **PIT_SAFE / REALISTIC / conservative**.
Zähleinheit: tägliche Beobachtung, maximal erster nativer Trigger pro
Strategie/Ticker/Session. Kandidat und SETUP_READY sind getrennte Zustände;
kein Anspruch unabhängiger Reset-/Cooldown-Events.

| Strategie | Geprüfte Aktien-Sessions | CANDIDATE | SETUP_READY | TRIGGERED | Vollständige True Trades | Sample |
|---|---:|---:|---:|---:|---:|---|
| Breakout | 480 | 111 | 14 | 0 | **0** | VERY_LOW_SAMPLE |
| EP | 480 | 0 | 0 | 0 | **0** | VERY_LOW_SAMPLE |
| Parabolic Short | 480 | 0 | 0 | 0 | **0** | VERY_LOW_SAMPLE |
| Parabolic Long | 480 | 0 | 0 | 0 | **0** | VERY_LOW_SAMPLE |
| Confluence Triggered | 480 | 57 | 0 | 0 | **0** | VERY_LOW_SAMPLE |

Die 14 Breakout-SETUP_READY-Beobachtungen verteilen sich auf BAC (acht Sessions,
31.08.–10.09.) und DJT (sechs Sessions, 23.09.–30.09.). Keine hatte in den
verfügbaren Bars einen nativen bestätigten ORH-Trigger, der zugleich den
bekannten Base-High überschritt. Fehlendes ORH-1m an diesen älteren Sessions
bleibt eine Auflösungsgrenze; es wird kein 1m-Trigger aus 5m rekonstruiert.
Die Aussage „null rekonstruierte Trigger“ bedeutet daher keine Widerlegung
von Ereignissen in nicht verfügbaren feineren Daten.

| Strategie | Ausschluss-/Screening-Grund | Häufigkeit |
|---|---|---:|
| Breakout | Kein bestätigter nativer Trigger bei SETUP_READY | 14 |
| Breakout | CANDIDATE, keine vollständige Setup-Freigabe | 111 |
| Breakout | Natives FAILED | 355 |
| EP | Natives FAILED, aktueller Gap unter unveränderter Schwelle | 480 |
| Parabolic Short | Native Extension-/Streak-Freigabe nicht erfüllt | 480 |
| Parabolic Long | Native Parabolic-/Breakdown-/Collapse-Freigabe nicht erfüllt | 480 |
| Confluence | Minervini FAIL | 370 |
| Confluence | Minervini PASS, aber Breakout nicht SETUP_READY | 110 |

Confluence im primären Forschungsfall:

* Minervini PASS + Breakout SETUP_READY: **0**.
* Davon tatsächlich ausgelöster Qullamaggie-Trigger: **0**.
* Davon vollständiger True Trade: **0**.

Minervini PASS lag an 110 Aktien-Sessions vor: davon 57 Breakout-CANDIDATE und
53 Breakout-FAILED. Alle 14 Breakout-SETUP_READY lagen bei Minervini FAIL.
Der Minervini-Filter wurde nicht als eigenständiger Entry interpretiert.

Ein historisches verifiziertes Catalyst-/Earnings-/Guidance-Archiv fehlt.
`INSUFFICIENT_CATALYST_DATA` ist daher als Gate implementiert und getestet;
im tatsächlichen Pilot gab es bereits keinen Gap-Kandidaten, sodass dieses Gate
hier **null individuelle Ausschlüsse** erzeugt. Rückblickende aktuelle Nachrichten
wurden nicht backdatiert. Historische Borrow-/Locate-Daten fehlen ebenfalls.

## Metriken, Kosten, Risiko und Monte Carlo

Alle fünf Strategien haben null vollständige True Trades. Win Rate, Average
Winner/Loser, Median Trade, Expectancy, Profit Factor, Mean/Median R und
durchschnittliche Haltedauer sind folglich **nicht bestimmbar**. Die
Gewinn-/Verlustserienzähler sind leer/0 und keine Erfolgskennzahl.
Die sechs Sensitivitätszellen (IDEAL/REALISTIC/STRESS, jeweils conservative/
optimistic) bleiben ebenfalls bei null Trades. Kein DATA_END-Trade wurde als
Nullrendite oder vollständiger Trade verbucht; es gab keine Entry-Pläne.

**Keine Strategie erreicht >=100 True Trades.** Die 0,25/0,50/1,00-%-Risikoläufe
sind für jede Strategie nur `TECHNICAL_FUNCTION_TEST / VERY_LOW_SAMPLE` mit
null Positionen. Ein unverändertes Startkapital dort ist keine Performance-
Beobachtung. Historische Strategie-Monte-Carlo-Pfade: **0**, für IID und Block
jeweils `INSUFFICIENT_TRUE_TRADES`. Die generische Prop-Challenge-Architektur
bleibt vorhanden; keine Firma wurde simuliert.

Es gibt **keine tatsächlichen Beispieltrades** für die gewünschte Tabelle
`Ticker | Datum | Trigger | Entry | Stop | Exit | R | P&L`. Beispiele wurden nicht
synthetisiert. Synthetische numerische Testfälle sind ausschließlich Unit-Tests,
kein Bestandteil dieser historischen Stichprobe.

## Verifikation und lokale Artefakte

Ausgangsstand: 979 Tests grün, ein Skip. Abschluss: **1.060 Tests grün, ein Skip**
(979 bestehende + 81 neue). Neue adversariale True-Trade-Tests prüfen
Range-Clocks, tatsächliches nächstes Open, bekannte Stops/Extrema, kausales VWAP,
Catalyst-/Volumen-Clocks, Exit-Reihenfolge, Long/Short-P&L, alle Kosten und R,
Time-Exit, PIT_SAFE, native Quellengleichheit, Portfolio-Limits und MC-Sperren.
Ein synthetischer 10.000-Pfade-IID-Test prüft das Endkapital gegen eine unabhängig
berechnete feste Risiko-Sequenz. Er ist kein historischer Strategie-Monte-Carlo.

Dateien dieser Erweiterung:

| Status | Datei | Zweck |
|---|---|---|
| neu | `intraday_audit.py` | Begrenzter Quellen-/Retention-Audit mit gespeicherten Rohantworten |
| neu | `true-trade-config.json` | Vorab fixierter Umfang, Kosten, Exit- und Risikoprotokoll |
| neu | `true_trade_collect.py` | Getrennte Akquisition und unveränderliche Input-Konfiguration |
| neu | `true_trade_replay.py` | Native kausale Setup-/Trigger-/Confluence-Rekonstruktion |
| neu | `true_trade_execution.py` | Nächstes Open, bekannte Stops, Exits, Kosten und Trade-Metriken |
| neu | `true_trade_portfolio.py` | Risiko-Book, >=100-Gate, Bootstrap und generischer Challenge-Adapter |
| neu | `true_trade_run.py` | Offline-Orchestrierung, Ledger, Sensitivitäten und Berichte |
| neu | `TRUE-TRADES.md` | Methodik, vollständiger Quellen-Audit und Reproduktion |
| neu | `TRUE-TRADES-FIRST-RUN.md` | Dieser kompakte tatsächliche Erstlaufbericht |
| neu | `../tests/test_true_trades.py` | 81 neue Tests einschließlich synthetischem 10.000-Pfade-Test |
| geändert | `SKILL.md` | Einstieg in den getrennten True-Trade-Workflow |
| geändert | `../README.md` | Auffindbarkeit und tatsächliche Pilotgrenzen |

Alle hier nicht mit `../` bezeichneten Pfade liegen in `trader-backtest/`.
Bestehende Strategiequellen, Konfigurationsparameter, Open-T+1-Engine und
`portfolio_challenge.py` wurden nicht geändert.

Der vollständige maschinenlesbare Lauf und Rohantworten bleiben ausschließlich
im ignorierten `trader-backtest/runs/true-trades-first/`:

* `audit/coverage-audit.json` plus acht Roh-/Fehlerantworten und Request-Hashes.
* `data/dataset.json`, eingefrorene Konfiguration und 24 Yahoo-Rohantworten.
* `results/trigger-replay.json`: alle Screening-, Trigger- und Coverage-Fälle.
* `results/trade-reconstruction.json`, `true-trades.csv.gz`: leeres Trade-Ergebnis
  mit vollständigem Schema; keine erfundenen Trades.
* `results/summary.json`, `REPORT.md`: Kennzahlen, Ausschlüsse, sechs Sensitivitäten.
* `results/portfolio/`: alle drei technischen Risikoläufe je Strategie sowie
  explizite Monte-Carlo-Nichtverfügbarkeitsberichte.

Grenzen: kleiner aktueller Survivor-Pilot und dessen kleine Rangpopulation,
begrenzte Intraday-Auflösung/-Historie, revidierbare Preis-Snapshots, kein
historisches Catalyst-/Identitäts-/Delisting-/Borrow-Archiv und modellierte
statt beobachteter Transaktionskosten. Weder Profitabilität noch eine Aussage
über 1993–2026 lässt sich daraus ableiten. Für mehrjährige Tests ist eine
lizenzierte historische US-1m-Quelle mit Corporate Actions, Vintages und
delisteten Titeln nötig; EP benötigt zusätzlich zeitgestempelte Catalyst-
Archive. Konkrete Datenanforderungen stehen in der Methodik.
