# True-Trade-Rekonstruktion v1

Dieser getrennte Intraday-Pfad ergänzt den bestehenden `trader-backtest`-Skill.
Er ruft die unveränderten nativen Strategie-, Stop-Risiko-, Exit- und
Confluence-Funktionen auf. Parameter und Daily-/Event-Studien bleiben unverändert.
Ein Daily-`SETUP_READY` ist eine Beobachtung, kein Trade.

Primär gelten `PIT_SAFE`, `REALISTIC` und `conservative`. Kein Broker, keine
Orders, keine Optimierung. Ergebnisse des tatsächlichen ersten Laufs stehen in
[TRUE-TRADES-FIRST-RUN.md](TRUE-TRADES-FIRST-RUN.md).

## Zuerst Abdeckung, dann Akquisition, dann Offline-Replay

Vom Repository-Root:

```bash
python3 trader-backtest/intraday_audit.py \
  --output trader-backtest/runs/true-trades-first/audit
python3 trader-backtest/true_trade_collect.py \
  --config trader-backtest/true-trade-config.json \
  --audit trader-backtest/runs/true-trades-first/audit/coverage-audit.json \
  --end 2026-11-12 \
  --output trader-backtest/runs/true-trades-first/data
python3 trader-backtest/true_trade_run.py \
  --config trader-backtest/true-trade-config.json \
  --dataset trader-backtest/runs/true-trades-first/data/dataset.json \
  --output trader-backtest/runs/true-trades-first/results
python3 -m unittest discover -s tests -q
```

Akquisition benötigt Netzwerk, Replay keines. Rohantworten einschließlich
Fehlerantworten, Request-Zeitpunkt, URL und SHA256 werden lokal gespeichert.
Ein bestehendes Audit wird unverändert wiederverwendet. Andere Konfigurationen,
Datenstände und neue Audits benötigen eigene Laufverzeichnisse. Im Ergebnis
stehen Input-, Konfigurations-, Implementierungs- und Strategie-Hashes.
`runs/` ist bereits durch `.gitignore` ausgeschlossen.

Das `--end` ist eine explizite abgeschlossene Exchange-Session. Der erste Lauf
verwendet die acht bereits in `config.first.json` vorab festgelegten Aktien;
kein Ticker wurde anhand eines Ergebnisses entfernt oder ergänzt. Das ist keine
Reproduktion des früheren 5.933-Aktien-Gesamtuniversums. Relative Ränge hängen
von dieser kleinen Population ab. Der Replay kann mit einem anderen vorab
fixierten, vollständig bereitgestellten Universum laufen; dafür müssen Daily-
und Intraday-Daten sowie die Rangpopulation zusammenpassen.

## Grenzen der zulässigen Quellen

Audit vom 13.11.2026, ein liquider Referenztitel (AAPL), jeweils eine jüngere
Anfrage und eine bewusst überlange diagnostische Anfrage. Kein Paging alter
Zeiträume, kein Hostwechsel, keine Umgehung von Retention-/Rate-Limits.

| Quelle | 1m | 5m | 15m | 60m | Reichweite / Nachweis |
|---|---|---|---|---|---|
| Yahoo Chart, inoffiziell | ja | ja | ja | ja | Beobachtete Grenzen siehe unten; keine 1993–2026-Intraday-Historie |
| Stooq | unbestätigt | unbestätigt | unbestätigt | unbestätigt | Kein validierter historischer US-Intraday-Adapter im Projekt; kein ungesicherter Ersatz |
| Nasdaq Symbol Directory | nein | nein | nein | nein | Symbolverzeichnis, keine OHLCV-Quelle |
| Yahoo RSS / trader-news | nein | nein | nein | nein | Aktuelle Meldungen, kein vollständiges historisches Catalyst-Archiv |
| SEC / trader-news | nein | nein | nein | nein | Filing-/Acceptance-Zeitstempel können Evidence liefern, keine Preisbars |
| trader-social | nein | nein | nein | nein | Historisches Archiv und Zugang hier nicht konfiguriert |
| Quiver / House-Disclosures | nein | nein | nein | nein | Disclosure-Kontext, kein Intraday-Preisfeed; API-Zugang nicht konfiguriert |

| Yahoo-Anfrage | Tatsächlich beobachteter Beginn UTC | Letzter Timestamp UTC | Abgelehnte überlange Anfrage |
|---|---|---|---|
| 1m, `range=7d` | 04.11.2026 09:00 | 13.11.2026 00:59:16 | `30d`: höchstens acht Tage 1m-Daten **pro Anfrage** |
| 5m, `range=60d` | 20.08.2026 08:00 | 13.11.2026 00:59:16 | `90d`: „within the last 60 days“ |
| 15m, `range=60d` | 20.08.2026 08:00 | 13.11.2026 00:59:16 | `90d`: „within the last 60 days“ |
| 60m, `range=730d` | 15.12.2023 09:00 | 13.11.2026 00:59:16 | `3y`: „within the last 730 days“ |

Die akzeptierten `range`-Antworten umfassen hier mehr Kalendertage als die
Fehlermeldungen nennen. Das ist ein dokumentiertes Anbieter-Verhalten, keine
garantierte Retention-Spezifikation. Insbesondere beweist „acht Tage pro
Anfrage“ keine exakte maximale Archivtiefe von 1m. Geprüft und genutzt wird nur
die tatsächlich gelieferte jüngere Antwort, ohne alte Zeitfenster abzufragen.
Für einen neuen Lauf muss der Audit wieder in einem neuen Verzeichnis erfolgen.

Bei allen vier Yahoo-Intervallen wurden mit `includePrePost=true` Pre-Market-
und After-Hours-Timestamps beobachtet. Quote-OHLC nutzt den bereits im Projekt
definierten splitadjustierten Yahoo-Preisvertrag, nicht dividend-adjustiertes
`adjclose`. Splits werden mit dem bestehenden Adapter auf die Replay-Basis
gebracht. Historische Vendor-Vintages oder Lieferzeitpunkte fehlen.
UTC-Epoch-Starts und `America/New_York` sind vorhanden; DST und Exchange-Kalender
bestimmen die Session, nicht das heutige pauschale GMT-Offset. Unfertige Bars,
Pre-/After-Hours und unregelmäßige Timestamps gehen nicht in die Opening Range.

Yahoo veröffentlicht für diesen inoffiziellen Endpoint kein verlässliches
Rate-Limit: serielle Requests mit 0,5 Sekunden Pause, Stopp bei 429/Auth-Fehlern,
keine Retries/Rotation. Andere Quellen haben keine nutzbaren Intraday-Bars:
Pre-/After-Hours und Splitadjustierung sind dort nicht anwendbar bzw. unbestätigt.
Nasdaq-, RSS-, Stooq- und öffentliche Quiver-Limits sind nicht verlässlich
bekannt. Für SEC gilt Fair Access mit maximal zehn Requests/Sekunde; der neue
Collector ruft SEC nicht auf. Social-Limits hängen vom nicht konfigurierten
Provider ab. Publikationszeiten von Meldungen allein beweisen keine damalige
Verfügbarkeit oder wirtschaftliche Catalyst-Bestätigung.

15m und 60m sind nur auditiert: Der vorhandene native Triggervertrag verarbeitet
1m/5m. Insbesondere liefert eine 60m-Bar weder die 1m-/5m-Trigger noch eine
genauere Ausführung nach dem 60m-Range-Ende. Es gibt keinen Daily-Fallback.
Der Status bleibt `INTRADAY_HISTORY_LIMITED`.

## Kausale Clock und Ereignisse

Die Daily-Basis endet vor dem Intraday-Tag am letzten abgeschlossenen Close T.
Sie benötigt den bestehenden gemeinsamen 260-Session-Datenkohort, einschließlich
historischer Rank-Population und nativer Minervini-Prüfung. Frühester Intraday-
Entry liegt am Folgetag; Close-T-Daten können keinen Entry am Open-T begründen.
Das bestehende separate Daily-Open-T+1-Modell bleibt unverändert.

Die Zähleinheit dieses Moduls ist die tägliche Kandidaten-/`SETUP_READY`-
Beobachtung. Sie ist ausdrücklich kein neu definiertes `FULL_RESET`- oder
`COOLDOWN20`-Event. Pro Strategie, Ticker und Session wird höchstens der erste
native bestätigte Trigger genutzt. Eine Serie aktiver Daily-Zustände kann daher
mehrere Tage umfassen; statistische Unabhängigkeit wird nicht behauptet.

Für eine Session wird 1m gewählt, wenn eine zeitgerechte erste 1m-Bar vorliegt,
sonst 5m. Kein Zusammenstückeln der Auflösungen und kein Wechsel anhand eines
späteren Preisverlaufs. 5m kann ORH/ORL-1m nicht auflösen. Nach der ersten Lücke
endet der bestätigbare Trigger-Prefix. Eine spätere Lücke löscht keinen schon
bestätigten früheren Trigger, kann aber seinen Exit unrekonstruierbar machen.
Korrekturen nach Bar-Ende ersetzen keine historische Execution-Bar.

* Breakout: natives vorheriges `SETUP_READY`; native ORH-1m/5m/60m erst nach
  Range-Abschluss, dann zusätzliche spätere Close-Bestätigung. Der bestätigte
  Close muss gemäß vorhandener Regel auch über `base_high` liegen. Das native
  Daily-Breakout bleibt Kontext (`current_entry=false`).
* EP: aktueller Gap gemäß unveränderter Schwelle, natives Volumen/RVOL bis zum
  jeweiligen Trigger, verifizierter kategorisierter Catalyst mit Referenz.
  Sowohl Publikation als auch Verfügbarkeit müssen strikt **vor** dem Trigger
  und damit vor Entry liegen. Disclosure-/Filing-/Revisionszeiten zählen mit.
  Späteres Volumen bestätigt keinen früheren Trigger. Fehlende oder nur
  retrospektive Evidence ergibt `INSUFFICIENT_CATALYST_DATA`, keinen EP-Trade.
* Parabolic Short: natives cap-aware `SETUP_READY`, dann ORL, erste rote 5m-Bar
  nach nativer Extension oder native VWAP-Crack/Bounce/Failure-Sequenz. VWAP
  verwendet ausschließlich abgeschlossene Prefix-Bars und deren Volumen.
* Parabolic Long: eigene native Breakdown-/Collapse-Basis; ORH bzw. erste grüne
  5m-Bar nach vorhandener Regel. Keine Umdeutung einer Short-Extension in Long.
* Confluence: vorheriges Minervini PASS + Breakout `SETUP_READY`, dann exakt
  derselbe bestätigte Qullamaggie-Trigger. Ein Plan enthält seine Breakout-
  Parent-ID. Minervini selbst erzeugt hier keinen Entry.

Zur getrennten Untersuchung von Short-Preisverläufen werden nur die vorhandenen
Paper-Risk-Berechtigungen `allow_short/allow_margin/paper` gesetzt; keine
Strategie-Schwelle ändert sich. Das ist keine historische Locate-Bestätigung.
Marktkapitalisierungen werden nur mit historischer Evidence-Verfügbarkeit
verwendet; andernfalls bleibt die native UNKNOWN_OR_MID-Kategorie erhalten.

## Entry, Stop und Exit

Entry ist ausschließlich das Open der unmittelbar folgenden tatsächlich
beobachteten Intraday-Bar: deren Start entspricht dem Ende der bestätigenden
Trigger-Bar. Kein Einstieg in die Trigger-Bar, zu deren Low/High oder zum OR-
Level. Fehlende nächste Bar oder Volumen null ergibt keinen Fill.

Stop: natives Session-Low für Long bzw. Session-High für Short **bis
einschließlich Trigger**, niemals das finale Tagesextrem. Bei VWAP_FAILURE
wird der zu diesem Zeitpunkt bekannte VWAP-Reclaim-Level als fester initialer
Stop verwendet, wie im bestehenden Trade-Simulator. Es wird kein dynamischer
zukünftiger VWAP-Stop erfunden. Unbekannter Stop oder dessen Zeitstempel ergibt
`INSUFFICIENT_STOP_DATA`. Das tatsächliche folgende Open muss mit dem Stop
vereinbar sein; ATR-/ADR-Präferenz und Warnschwelle werden auch an diesem Open
mit den bereits bekannten Daily-Volatilitätswerten geprüft.

Cent-Rundung und 2R-Target kommen aus `trader-exit.compute` am tatsächlichen
Entry-Open, Stop und Basis bleiben bekannt. Wie im bestehenden Pilot gilt
maximal eine Session, Flat-by 15:55 ET bzw. spätestens fünf Minuten vor Early
Close. Time-Exit nutzt das tatsächlich beobachtete Open dieser Cutoff-Bar.

Exit-Gründe sind `STOP_EXIT`, `TARGET_EXIT`, `TIME_EXIT`, `TRAILING_EXIT`,
`DATA_END` und `INSUFFICIENT_EXIT_DATA`. Die native Strategie hat
`automatic_exit=false` für den Trailing-Kontext; v1 erzeugt daher keine
automatischen `TRAILING_EXIT`-Trades und ergänzt keine neue Regel.
Ein Stop-Gap füllt zum ungünstigen Open, ein Target-Gap höchstens zum Target.
Liegt das Open bereits eindeutig an einem Level, ist diese Reihenfolge bekannt.
Andernfalls markiert eine Berührung beider Levels in derselben OHLC-Bar
`AMBIGUOUS_INTRABAR_ORDER`: primär Stop zuerst, separat Target zuerst als
`optimistic`-Sensitivität. Intrabar-Exit-Zeiten werden als Intervall gespeichert;
für Cash-Freigabe/Haltedauer gilt konservativ dessen Bar-Ende.
Fehlende oder volumenlose Exit-Bars erzeugen keinen behaupteten Fill.

Ein unvollständiger Exit bleibt mit Status im Audit und wird aus vollständigen
Trade-Metriken ausgeschlossen, niemals als Null-P&L eingesetzt. Dieser
Complete-Outcome-Vorbehalt und seine Häufigkeiten stehen im Bericht.

## Kosten, Datensatz und Statistik

Alle Kosten sind die unveränderten, vorab fixierten Pilotannahmen:
IDEAL null; REALISTIC voller Spread 10 bp, Slippage je Seite 5 bp und 0,01 USD
Roundtrip-Gebühr/Aktie; STRESS doppelter Spread und doppelte Slippage.
Die Hälfte des Spreads und die jeweilige Slippage werden an beiden
beobachteten Fill-Basispreisen separat belastet. Sie sind **keine gemessenen
historischen Spreads/Gebühren**. Unbekannte Borrow-/Locate-Kosten werden nicht
geschätzt; Short-Preisrekonstruktionen tragen die entsprechenden Flags und
haben keine vollständig bekannten realen Netto-Ausführungskosten.

`true-trades.csv.gz` enthält alle drei Kosten- und beide Reihenfolge-Szenarien
mit `id`, Ticker/Strategie, Setup-Datum, Trigger-Typ/-Zeit, Entry-Zeit/-Preis,
initialem Stop/Target, Stop-Basis/-Zeit, Exit-Zeit/-Preis/-Grund, Seite,
Spread/Slippage/Gebühren/Gesamtkosten, Brutto-/Netto-P&L, R, Haltedauer,
PIT-/Datenqualitäts-/Catalyst-Status und Ambiguitätsflags. Jede Zeile ist eine
Aktie in USD, damit Positionierung und reine Trade-Qualität getrennt bleiben.
`trade-reconstruction.json` bewahrt zusätzlich unvollständige Versuche und
Intraday-Pfade; `trigger-replay.json` alle Screening-/Trigger-/Coverage-Fälle.
Gleiche IDs über Szenarien sind Sensitivitäten desselben Trades, keine neuen
Samples. Confluence ist eine Teilmenge mit Parent-Referenz, kein unabhängiger
zusätzlicher Breakout.

R verwendet wie die vorhandene Infrastruktur das initiale Stop-Risiko
einschließlich modellierter Roundtrip-Kosten. `gross_r_multiple` und die
reine tatsächliche Stop-Distanz werden separat gespeichert. Die statistische
Ein-Aktien-P&L ist keine portfolio- oder notionalgewichtete Rendite.
Pro Strategie werden Anzahl, Win Rate, Average Winner/Loser, Median Trade,
Expectancy, Profit Factor, Mean/Median R, maximale Gewinn-/Verlustserien und
mittlere Haltedauer ausgegeben. Bei null Trades bleiben die Kennzahlen null/
undefiniert; Profit Factor ohne Verlierer ist ausdrücklich undefiniert.

Sample-Status: unter 30 `VERY_LOW_SAMPLE`, 30–99 `LOW_SAMPLE`, ab 100
`EXPLORATORY_SAMPLE`. Kein Zwang zu einer Profitabilitätsaussage.

## Risiko, Monte Carlo und Challenge-Anschluss

Die Positions-Basisgröße ist `floor(Equity × Risk % / tatsächliche Stop-Distanz)`
für 0,25/0,50/1,00 %. Danach greifen Cash, Kosten, maximal fünf Positionen,
Ticker-Eindeutigkeit, maximal 3 % initiales Gesamtrisiko und höchstens 100 %
Gross-Exposure ohne Hebel. Costs vermindern Equity und Buying Power. Die
Grenzen gelten bei Admission; Stop-Gaps werden nicht auf das Budget gekappt.
Shorts benötigen zusätzlich verifizierte historische Borrow-Daten und
150-%-Collateral; Verkaufserlöse erzeugen keine freie Buying Power.

Unter 100 vollständigen True Trades sind die drei Risikoläufe ausschließlich
`TECHNICAL_FUNCTION_TEST` mit dem Sample-Status; keine Strategie-Monte-Carlo-
Pfade. Ab 100 gelten mindestens 10.000 `TRADE_IID`- und 10.000 `SESSION_BLOCK`-
Pfade, sofern mindestens zwei vorab festgelegte Fünf-Session-Blöcke existieren.
Vorhandene Timeline-, Block-Sampling-, Trade-Shifting-, Drawdown- und Bootstrap-
Summary-Funktionen werden wiederverwendet; Positionsgrößen und Caps werden
auf jedem Pfad neu angewendet. Ganze Intraday-Verläufe werden mitgenommen,
Session-Blöcke erhalten Cluster und Leertage. Keine neue Preisprognose.
Fehlendes Borrow-Archiv sperrt die entsprechende Short-Netto-Simulation.

Monte Carlo berichtet Endkapital-Median und P05/P50/P95, negative
Gesamtperformance, MaxDD-Quantile einschließlich P95, P(DD >5/10/20/30 %),
maximale Verlustserien und P(Gewinn >=25/50/100 %). Drawdowns basieren auf
beobachteten Intraday-Marks und Fills, nicht einer erfundenen Tick-Reihenfolge.
Am Resampling-Horizont noch offene Positionen bleiben markiert und gezählt.

`challenge_observations()` liefert die Curve mit expliziten Tagesanfängen und
tatsächlichem Entry-Risiko an das unveränderte generische
`portfolio_challenge.evaluate_challenge`. Keine Firma wird simuliert.
`first_hit()` und sein expliziter No-Hit-Sentinel bleiben unverändert.

## Datenqualität und Erweiterungsbedarf

Die bestehende kausale OHLCV-Prüfung über die 260 vergangenen Sessions ist das
primäre Admission-Kriterium. Am Trigger werden zusätzlich nur die bereits
abgeschlossenen Intraday-Bars auf numerische und Zero-Volume-Preisänderungs-
Probleme geprüft. Retrospektive `VALID_ONLY`-Labels, spätere SEC-/Identitäts-
Metadaten und spätere Outcome-Bars filtern keine Entries. Alle geprüften Ticker
bleiben im Audit. `PIT_SAFE` zertifiziert dabei die Kausalität der Auswahl,
keine unveränderliche damalige Vendor-Lieferung. Heutige Yahoo-Snapshots bleiben
`NOT_HISTORICALLY_PIT_CERTIFIED`; Survivorship-/Identitätsrisiken bestehen.

Für mehrere belastbare Jahre braucht es einen lizenzierten historischen US-
1m-OHLCV-Feed mit dokumentierter Retention, Session-/Timezone-Definition,
Corporate Actions, historischen Vintages und delisteten Symbolen, z.B. einen
entsprechend lizenzierten Databento-/NYSE-TAQ- oder vergleichbaren Archivvertrag.
Verfügbarkeit, Berechtigungen und genaue Zeitabdeckung sind vor Anschluss zu
prüfen; keine dieser Quellen ist hier angeschlossen. Für EP kommen historische
Earnings-/Guidance-/Catalyst-Publikations- und Verfügbarkeitsarchive hinzu;
für realitätsnahe Kosten NBBO/Spreads und für Shorts Borrow/Locates.

Es gibt keine Ableitung von dieser jüngeren Stichprobe auf 1993–2026.
