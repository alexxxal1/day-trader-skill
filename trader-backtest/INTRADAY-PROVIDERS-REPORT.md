# Abschlussbericht: modularer Intraday-Provider-Layer

Korrekturstand: **16.11.2026**. Zuerst `git status --short` geprüft und auf dem
vorhandenen uncommitteten Provider-Layer weitergearbeitet. Kein Reset, Fetch,
erneutes Aufsetzen oder Überschreiben des Arbeitsstands. Die vier Review-
Befunde sind mit vorher fehlschlagenden Regressionen korrigiert; Details und
Datei-/Zeilenangaben stehen in Abschnitt 13. Diese Korrektur ist ausschließlich
offline getestet, mit gesperrten Socket-Verbindungen.

Basis bleibt `55c968c54136935e714ac7db00fd3d310b1bce08`; kein Commit oder Push.
Der ursprüngliche Fetch und Fast-forward stammen aus der Erstimplementierung
vom 13.11.2026. Die Live-/Secret-Befunde in Abschnitten 4-11 stammen ebenfalls
aus diesem früheren Lauf und wurden am 16.11. nicht erneut live geprüft.

## 1. Erstellte und geänderte Dateien

25 Dateien insgesamt gegenüber HEAD: 20 neu, fünf geändert. Gegenüber dem
vorhandenen Provider-Layer kamen zwei Dateien hinzu: Basisprüfung und Review-Tests.

| Status | Datei | Zweck |
|---|---|---|
| Neu | `.gitignore` | Python-Caches und lokale Secret-Dateien ignorieren |
| Geändert | `README.md` | Provider-Layer verlinken und einordnen |
| Geändert | `trader-backtest/.gitignore` | Intraday-Cache ignorieren |
| Neu | `trader-backtest/intraday-provider-config.json` | Priorität, Feed, Adjustment, lokale Request-Limits |
| Neu | `trader-backtest/intraday_providers/__init__.py` | Öffentliche Provider-API |
| Neu | `trader-backtest/intraday_providers/model.py` | Request-/Bar-Schema, Zeitzonen, Sessions, Validierung |
| Neu | `trader-backtest/intraday_providers/base.py` | Gemeinsamer Vertrag, Pagination, Status, Checkpoints |
| Neu | `trader-backtest/intraday_providers/basis.py` | Snapshot-/Ledger-Nachweise und explizite Kursbasis-Konvertierung |
| Neu | `trader-backtest/intraday_providers/transport.py` | HTTPS, Auth-Abbruch, Rate-Limits und Retry |
| Neu | `trader-backtest/intraday_providers/cache.py` | Atomischer Cache mit Seitenprüfsummen |
| Neu | `trader-backtest/intraday_providers/alpaca.py` | Offizielle historische Stocks Bars, IEX/SIP |
| Neu | `trader-backtest/intraday_providers/yahoo.py` | Adapter über unverändertem Yahoo-Abruf |
| Neu | `trader-backtest/intraday_providers/massive.py` | Aggregates und vorbereitete Reference-Methoden |
| Neu | `trader-backtest/intraday_providers/registry.py` | Konfigurierbarer Fallback pro Aktie/Session |
| Neu | `trader-backtest/intraday_providers/provenance.py` | Provider-/Feed-/Adjustment-Konsistenz |
| Neu | `trader-backtest/provider_collect.py` | Acquisition auf eingefrorenem Daily-Kontext, Resume |
| Neu | `trader-backtest/provider_audit.py` | Capability-/Free-/Historien-Probes |
| Neu | `trader-backtest/provider_pilot.py` | Vergleich derselben eingefrorenen Setup-ID-Kohorte |
| Geändert | `trader-backtest/true_trade_replay.py` | Quellenfelder bis Trigger prüfen und weitergeben |
| Geändert | `trader-backtest/true_trade_execution.py` | Provider-Mixing bis Exit ablehnen |
| Geändert | `trader-backtest/true_trade_run.py` | Providerneutrale Reports und Quellen im Ledger |
| Neu | `tests/test_intraday_providers.py` | 104 neue Offline-Tests |
| Neu | `tests/test_intraday_provider_review.py` | 27 Review-/Grenzfall-Tests zu den vier Befunden |
| Neu | `trader-backtest/INTRADAY-PROVIDERS.md` | Vertrag, Grenzen, Betrieb und Reproduktion |
| Neu | `trader-backtest/INTRADAY-PROVIDERS-REPORT.md` | Dieser Bericht |

## 2. Tests und Bestandsschutz

Vollständiger Lauf am 16.11.2026 mit Python 3.12.14: unittest-Discovery für
`test_*.py`, ausgeführt durch den mitgelieferten Offline-Runner. Er sperrt
`socket.connect`, `connect_ex` und `create_connection` vor der Discovery.

- **1.191 Tests insgesamt: 1.190 bestanden, ein optionaler Test übersprungen.**
- Laufzeit: 37,447 Sekunden. 1.060 Tests aus HEAD plus 104 Provider-Tests und
  27 neue Review-/Grenzfall-Tests.
- Alle 81 bestehenden True-Trade-Tests bestehen; ihre Datei ist unverändert.
- Übersprungen:
  `test_event_portfolio.EmpiricalEventBootstrap.test_seed_is_independent_of_worker_thread_count`.
  Bestehender Dekorator in `tests/test_event_portfolio.py:149`:
  `skipUnless(ACCELERATOR=="numba", "optional compiled accelerator")`.
  Numba ist nicht installiert; `ACCELERATOR="python"`. Ohne den optionalen
  Accelerator ist der Test verschiedener Numba-Worker-Thread-Anzahlen nicht
  anwendbar. Allgemeine Seed-Reproduzierbarkeit wird separat getestet.
- `git diff --check`, vollständige Patch-Anwendbarkeit und unveränderte
  Strategie-/Parameter-Hashes zusätzlich geprüft.
- SHA256 aller Strategiequellen entsprechen weiterhin `strategy-sources.json`.
- `true-trade-config.json`, originale Yahoo-Akquisition, Open-T+1-Portfolio-Book
  und vorhandenes Monte-Carlo-Modul sind gegenüber `origin/main` unverändert.

Bestand bestätigt: historical true-trade reconstruction, `trader-backtest`,
PIT_SAFE, kausale Open-T+1-/Intraday-Ausführung, Portfolio und Monte Carlo.
Keine Strategieparameter optimiert oder native Strategiequellen geändert.

Neue Tests prüfen alle geforderten Adapter-/Auth-/Feed-Fälle, Pagination,
401/403/429/5xx, leere Antworten, Fallback, Mixing, UTC/New York, DST,
Premarket/Regular/Afterhours, frühen Börsenschluss, Cache-Hit/-Miss,
Seiten-/Session-Resume, persistiertes Retry-After und Datenqualität.
Zusätzlich: Providerwechsel nach Exit darf frühere Trades nicht verwerfen;
spätere ungültige Preise dürfen keinen früheren Provider-Fallback auslösen.

## 3. Provider-Architektur

`IntradayProvider` -> Yahoo/Alpaca/Massive -> normalisierte Bars -> bestehendes
`MarketData` -> unveränderte native Strategie-Funktionen.

Default-Reihenfolge: **ALPACA -> YAHOO -> MASSIVE**. Feed-Default: **IEX**.
Ein Konfigurationswechsel reicht für MASSIVE -> ALPACA -> YAHOO.
Keine Strategieänderung ist zum Ergänzen eines Adapters erforderlich.

Ein Aktie/Session-Bundle verwendet genau einen Provider. Ein vorhandenes
gültiges Opening wird nicht wegen späterer Fehler oder fehlender anderer
Frames verworfen; ein gesperrtes 1m-Fenster verhindert den zulässigen 5m-Abruf
nicht. Bereits gewählte Provider bleiben über Resume im Manifest gepinnt.
Innerhalb eines
rekonstruierten Trades werden Provider, Feed und Adjustment bis zum Exit
geprüft. Ein Wechsel ergibt `MIXED_PROVIDER_DATA` und `true_trade=false`.
Keine Interpolation, kein Stitching und keine Auswahl anhand späterer Renditen.

Cache-Key enthält Provider, Feed, Ticker, Timeframe, Start, End und Adjustment.
Seitenmanifest/Cursor und Session-Checkpoints erlauben Resume; Errors bleiben
nachweisbar. Vollständige, integritätsgeprüfte Cache-Einträge sind vor dem
Netzwerk-Preflight lesbar; unvollständige Einträge benötigen weiter gültige
Download-Voraussetzungen. Große Daten und generierte Reports sind ignoriert.

Raw und split-adjustierte Quelle werden nur mit expliziten, an konkrete Bars
und Ledger gebundenen Daily-Basisnachweisen übernommen. Split-adjustierte
Quelle benötigt zusätzlich ihren eigenen vollständigen Basisnachweis.
Unterschiedliche, nachgewiesene Split-Anker werden rechnerisch auf die
eingefrorene Daily-Basis abgebildet. Unbekannte oder widersprüchliche Basis
erzeugt einen expliziten Ausschluss, keine primären True Trades. Alte neue
Collector-Artefakte mit bloßem `FROZEN_DAILY_SPLIT_BASIS` ohne Verifikation
werden nicht als verifiziert akzeptiert.

## 4. Yahoo-Capability: tatsächlich geprüft

Vier kleine Live-Anfragen für AAPL am letzten abgeschlossenen US-Handelstag
**12.11.2026**, jeweils **09:30-10:30 America/New_York**:

| Timeframe | HTTP | Bars | Erste Bar UTC | Letzte Bar UTC | Qualität |
|---|---:|---:|---|---|---|
| 1m | 200 | 60 | 2026-11-12 14:30 | 2026-11-12 15:29 | VALID |
| 5m | 200 | 12 | 2026-11-12 14:30 | 2026-11-12 15:25 | VALID |
| 15m | 200 | 4 | 2026-11-12 14:30 | 2026-11-12 15:15 | VALID |
| 60m | 200 | 1 | 2026-11-12 14:30 | 2026-11-12 14:30 | VALID |

UTC/New York und reguläre Session stimmen. Premarket/Afterhours sind vom
Adapter unterstützt, wurden in diesen regulären Testfenstern nicht beobachtet.
Split-adjustierte Yahoo-Quotes bleiben erhalten; Raw wird nicht behauptet.
Feed bleibt `UNSPECIFIED`. Die kostenlose Quelle bleibt
**INTRADAY_HISTORY_LIMITED**.

Die Tabelle belegt nur diese Fenster, keine vollständige früheste Historie.
Die zuvor dokumentierten Grenzen bleiben erhalten; kein Retention-Bypass.
Tatsächliches Account-/Server-Rate-Limit ist unbekannt, lokal 120 Requests/Minute
mit unveränderter Stop-Regel bei Rate-/Auth-Fehlern.

Lokale, ignorierte Nachweise:

- `runs/intraday-provider-audit/first-live-provider-capability-audit.json`:
  erste vier HTTP-200-Ergebnisse.
- `runs/intraday-provider-audit/provider-capability-audit.json`:
  aktueller Capability-Audit; erneuter Abruf ausschließlich aus Cache.
- `runs/intraday-provider-audit/cache/`: normalisierte Seiten und ursprüngliche
  immutable Yahoo-Rohantworten mit Request-Metadaten/Prüfsummen.

## 5. Alpaca-Capability

Adapter vollständig implementiert und offline getestet: Market Data REST,
Auth-Header, alle vier Timeframes, vollständige `next_page_token`-Pagination,
Raw/Split, IEX/SIP, Session-/Zeitstempel-Normalisierung, bounded Retry.
60 Minuten werden gemäß offizieller REST-Spezifikation als `1Hour` übertragen;
der interne Alias `60Min` wird unterstützt.

Aktueller Status: **DATA_UNAVAILABLE / SKIPPED_MISSING_AUTH**.
Provider: ALPACA; konfigurierter Feed: IEX. SIP-Berechtigung ungetestet.
Früheste/späteste lieferbare Historie, echte Historientiefe und tatsächliche
Account-Limits: **unbekannt**. Lokaler Client: 180 Requests/Minute,
maximal drei Retries. Keine Alpaca-Anfrage wurde gesendet.

## 6. Massive-Capability

Adapter vollständig offline getestet: Auth, Aggregates 1/5/15/60 Minuten,
Raw/Split, Parsing, Zeitzonen, Sessions, `next_url` und fehlender Key.
Reference-Methoden für aktive/delistete Ticker, Details as-of, Splits und
Ticker Events sind vorbereitet, ohne Einfluss auf Strategien.

Aktueller Status: **DATA_UNAVAILABLE**, Auth fehlt, **null API-Anfragen**.
Historientiefe, konkrete Plan-Berechtigungen, früheste/späteste lieferbare Daten
und tatsächliche Account-Limits sind unbekannt. Lokaler Client-Default:
fünf Requests/Minute. Kein Kauf, Abo oder kostenpflichtiger Download.

## 7. Vorhandene Alpaca-Secrets

Sowohl Umgebungs-Konfiguration als auch tatsächliche ENV-Präsenz geprüft:

| Secret | Vorhanden |
|---|---|
| `ALPACA_API_KEY` | Nein |
| `ALPACA_API_SECRET` | Nein |
| `MASSIVE_API_KEY` | Nein |

Keine Secret-Werte ausgegeben oder im Repository gespeichert.

## 8. Historische Jahre, Feed und Pilot

Kein Alpaca-Zugang vorhanden. Deshalb **keine live bestätigten Jahre** aus
2026/2025/2024/2022/2020/2018/2016; auch keine belegte Delisted-/Alt-Ticker-
Verfügbarkeit. IEX ist der Default, kein erfolgreicher Live-Feed-Nachweis.
SIP ist nicht getestet. Dokumentation wurde nicht als Messung ausgegeben.

Der Free-Live-Test und die anschließenden kleinen Historien-Probes sind als
ausführbarer Audit vorbereitet. Der Alpaca-True-Trade-Pilot wurde aufgrund
fehlender Secrets und des damit fehlenden bestandenen Live-Tests nicht
ausgeführt. Anzahl rekonstruierbarer Alpaca-True-Trades: **unbekannt**, nicht null.

## 9. Yahoo vs Alpaca

Ein neuer gepaarter Vergleich ist noch nicht möglich. Das frühere Yahoo-
Ergebnis stammt ausdrücklich aus dem vorhandenen
`TRUE-TRADES-FIRST-RUN.md`, nicht aus einem neuen Provider-Pilot:

| Strategie | Früherer Yahoo SETUP_READY | Yahoo Trigger | Yahoo True Trades | Alpaca |
|---|---:|---:|---:|---|
| Breakout | 14 | 0 | 0 | Nicht getestet |
| EP | 0 | 0 | 0 | Nicht getestet |
| Parabolic Short | 0 | 0 | 0 | Nicht getestet |
| Parabolic Long | 0 | 0 | 0 | Nicht getestet |
| Confluence Triggered | 0 | 0 | 0 | Nicht getestet |

Der ursprüngliche Pilot umfasst acht Aktien, 20.08.-12.11.2026. Der neue
Vergleichsrunner friert dessen SETUP_READY-IDs ein und berichtet für dieselben
IDs Datenverfügbarkeit, Trigger, True Trades und Ausschlussgründe. Der
ursprüngliche eingefrorene Yahoo-Datensatz ist ein ignoriertes Run-Artefakt
und muss für die spätere exakte Reproduktion vorhanden sein; eine neue
Yahoo-Abfrage darf ihn nicht stillschweigend ersetzen.

Es gibt derzeit keinen Nachweis, dass Alpaca mehr Trigger rekonstruiert.
Keine Profitabilitätsaussage oder Strategieoptimierung.

## 10. Benötigte Netzwerk-Domains

- **Neu für Alpaca:** `data.alpaca.markets`.
- **Später für Massive:** `api.massive.com`.
- **Yahoo:** `query2.finance.yahoo.com`; `query1.finance.yahoo.com` bleibt
  für bestehende andere Yahoo-Workflows relevant.

Die aktuelle Cloud-Allowlist erlaubt Yahoo; Alpaca und Massive sind bislang
nicht freigeschaltet. Kein API-Zugriff wurde ohne Key versucht. Die
Allowlist wurde nicht verändert. Es wird keine Broker-Domain benötigt.

## 11. Benötigte Secrets

Alpaca: `ALPACA_API_KEY`, `ALPACA_API_SECRET` ausschließlich über ENV.
Massive optional später: `MASSIVE_API_KEY` ausschließlich über ENV.
Yahoo benötigt kein Secret. Kein Broker-Setup und keine Order-Berechtigung.

## 12. Benötigen wir Massive bereits?

**Für den nächsten Schritt noch nicht.** Zuerst Alpaca IEX mit kostenlosen
begrenzten Live-Fenstern und anschließend dem identischen Setup-Pilot prüfen.
Erst dessen tatsächlich gemessene Abdeckung, Datenlücken und Identitätsgrenzen
zeigen, ob Massive für längere Historie oder Reference-Daten erforderlich ist.
Die technische Vorbereitung ist vorhanden; eine Entscheidung für einen Tarif
oder eine bestimmte Historientiefe wird daraus nicht abgeleitet.

Kein Commit, kein Push, keine Orders, keine Alpaca-Brokerverbindung,
kein Massive-Abo und keine großen kostenpflichtigen Downloads.
Weitere Aktionen warten auf Freigabe.

## 13. Vier Review-Befunde: Reproduktion, Ursache und Korrektur

Vor jeder Produktionskorrektur wurden die 14 initialen Review-Tests ausgeführt:
**11 fehlschlagend, drei bestehende Checkpoint-Schutzfälle bereits grün**.
Das unveränderte Vorher-Protokoll liegt im Prüfpaket als
`regressions-before.log`. Nach den Korrekturen bestehen diese Fälle sowie
13 weitere Grenzfälle: **27 Review-Tests grün**, `regressions-after.log`.

### Befund 1: späteres Duplikat änderte frühere Providerwahl

Reproduktion: Bei identischem Anfang wählte der Router ohne späteres
Duplikat ALPACA, mit Duplikat YAHOO. Im nativen synthetischen Replay
verschwand dadurch auch ein früherer Breakout-Trigger und abgeschlossener
Trade. Regressionen: `tests/test_intraday_provider_review.py:107` und `:117`.

Ursache: Ein Strukturfehler setzte die gesamte Antwort auf `INVALID_DATA`;
der Router verwarf zudem Bundles mit einem fehlerhaften anderen Frame.

Korrektur:

- `intraday_providers/model.py:145`: Validierung in unveränderter
  Antwortreihenfolge. Bereits gültige Beobachtungen bleiben unverändert.
  Fehlerhafte strukturelle Beobachtung und Suffix werden quarantänisiert;
  numerische Fehler bleiben explizite Lücken. Keine Interpolation, kein
  nachträgliches Sortieren oder Ersetzen früherer Beobachtungen.
- `intraday_providers/base.py:115`: Verfügbarkeit aus nutzbaren Beobachtungen
  getrennt vom weiterhin ungültigen Qualitätsstatus ableiten. Cutoff und
  Quarantäne bleiben sichtbar. `:141` erhält auch vor einem späteren
  Pagination-Fehler geladene, validierte Beobachtungen unter gültigen
  Download-Voraussetzungen; Abschluss bleibt dabei false.
- `intraday_providers/registry.py:34`: Opening-Eignung entscheidet; spätere
  Fehler anderer Frames verwerfen das Opening nicht. `selection_basis`
  dokumentiert die Wahl. Fehlerstatus eines Teildownloads bleibt getrennt.
- `provider_collect.py:53`: Provider im Fortschrittsmanifest pinnen. Resume
  darf auch bei verlorener Auth nicht auf einen anderen Provider umschalten.

Geprüft: gleiche Providerwahl, identischer nativer Trigger und identischer
abgeschlossener Trade mit/ohne späteres Duplikat; Quarantäne des Suffixes;
späterer Seitenfehler; fehlendes Opening; gepinnter Resume. Keine ungültige
Beobachtung oder gemischte Quelle wird übernommen.

### Befund 2: Yahoo-5m wurde nach gesperrtem 1m nicht angefragt

Regression: `tests/test_intraday_provider_review.py:145`.
Die Vorher-Version lieferte keinen Provider und führte keine 5m-Anfrage aus.

Ursache: `registry.py` brach die Frame-Schleife beim ersten Fehlerstatus ab
und verlangte anschließend Erfolg aller angefragten Frames.

Korrektur: `intraday_providers/registry.py:34` prüft die weiteren Frames
desselben Providers und lässt einen gültigen Opening-Frame zu. Im Test wird
1m lokal mit `INTRADAY_HISTORY_LIMITED` abgelehnt; nur die erlaubte 5m-Anfrage
geht an den injizierten Offline-Requester. Der bestehende `session_rows`-
Mechanismus wählt weiterhin 1m bei vorhandenem 1m-Opening, sonst 5m.
Ein zusätzlicher Collector-Test bestätigt 5m mit nachgewiesener Kursbasis.
Kein Trigger oder Ergebnis wird zur Frame-Auswahl benutzt.

### Befund 3: Preflight sperrte bereits vollständige Cache-Daten

Regressionen: `tests/test_intraday_provider_review.py:187` und `:201`.
Vorher ergab der vollständige Yahoo-Cache nach Ablauf des Fensters
`INTRADAY_HISTORY_LIMITED`; Alpaca ohne aktuelle Auth `DATA_UNAVAILABLE`.

Ursache: Auth-/Historien-Preflight lief vor dem Cache-Zugriff.

Korrektur: `intraday_providers/base.py:78` lädt und prüft den Cache zuerst.
Nur ein explizit vollständiger Eintrag umgeht den Netzwerk-Preflight und
führt keinerlei Anfrage aus. `intraday_providers/cache.py:28` prüft Key,
Seitenprüfsummen, Bar-Anzahl und konsistenten Abschluss ohne Rest-Cursor.

Geprüft: vollständiger Yahoo-Cache nach Ablauf des Fensters, Alpaca-/Massive-
Cache ohne Auth, Integritätsfehler trotz fehlender Auth. Unvollständige
Checkpoints benötigen weiter gültige Auth/Retention; auch eine gespeicherte
letzte Seite mit `complete=false` darf dies nicht umgehen. Aktive Retry-After-
Sperren für unvollständige Downloads bleiben wirksam.

### Befund 4: fremde Split-Basis wurde still als Daily-Basis bezeichnet

Regressionen: `tests/test_intraday_provider_review.py:310`, `:315`, `:323`,
`:336` und `:350`. Vorher wurde eine nach einem späteren 2:1-Split bereits
auf 50 adjustierte historische Intraday-Quote unverändert als zur eingefrorenen
Daily-Basis 100 gehörig bezeichnet. Fehlende und widersprüchliche Nachweise
führten ebenfalls nicht zum Ausschluss.

Ursache: Nur Raw wurde anhand des eingefrorenen Ledgers umgerechnet;
split-adjustierte Daten erhielten ohne Kompatibilitätsprüfung das gleiche
Speicherbasis-Label.

Korrektur:

- `intraday_providers/basis.py:20`: Explizite Snapshot-/Ledger-Nachweise mit
  Ankerdatum, Vollständigkeitsfenster, Evidence-Referenz und Hash-Bindung an
  tatsächliche Bars und Ledger verlangen. Keine Ableitung aus Providername,
  Abrufdatum, gleichen Preisen oder einem leeren Ledger.
- `intraday_providers/basis.py:51`: Überlappende Actions auf Widersprüche
  prüfen; Quelle mit `F_source / F_daily` auf die eingefrorene Basis abbilden.
  OHLC/VWAP werden mit diesem Faktor, Volumen mit dem inversen umgerechnet.
  Raw hat `F_source=1`; ein späterer Split wird nicht in den alten Ledger
  hineinerfunden. Beide Konvertierungsrichtungen sind getestet.
- `provider_collect.py:62`: Nur verifizierte Umrechnung speichern; Quell-
  Adjustment und tatsächliche Speicherbasis samt IDs/Faktor trennen. Ist
  der zuvor gewählte Opening-Frame nicht verifizierbar, Session explizit
  ausschließen statt einen anderen Provider oder Frame zu wählen.
- `intraday_providers/provenance.py:6`, `true_trade_replay.py:127` und
  `true_trade_execution.py:128`: Auch falsch umgelabelte Alt-Artefakte ohne
  Verifikation sowie unterschiedliche Speicherbasis-IDs zwischen Trigger
  und Exit blockieren. Prüfungen beziehen sich auf das jeweils benötigte
  Präfix; ein späterer Basisfehler nach Exit verwirft keinen früheren Trade.

Explizite Ausschlüsse: `BASIS_COMPATIBILITY_UNKNOWN`,
`BASIS_EVIDENCE_MISMATCH`, `BASIS_INCOMPATIBLE`. Geprüft sind gleiche Basis,
unterschiedliche nachgewiesene Basis, Split zwischen den Snapshots, Raw,
unbekannte Basis, unvollständiges Ledger, falsche Hash-Bindung und Konflikte.
Unverifizierte Zeilen erzeugen weder native Pläne noch primäre True Trades.

## 14. Verbleibende Grenzen und Prüfpaket

- Diese Korrektur enthält ausschließlich Offline-Tests; keine neue Messung
  von Alpaca-/Massive-Zugang oder Historientiefe und keinen Live-Pilot.
- Antwortreihenfolge und Bar-Close-Verfügbarkeit sind weiterhin dokumentierte
  Snapshot-Annahmen, keine historischen Delivery-/Revision-Vintages.
- Hashes belegen Bindung und Konsistenz der angegebenen Evidenz. Sie beweisen
  nicht eigenständig die sachliche Vollständigkeit eines vom Lieferanten
  gelieferten Split-Ledgers. Archivierte Basisnachweise müssen tatsächlich
  vorhanden und zutreffend sein. Ohne sie bleibt die Session ausgeschlossen.
- Alte Daily-Snapshots ohne expliziten Basisnachweis können nicht still als
  mit einem späteren Intraday-Snapshot kompatibel gelten. Neue Akquisition
  benötigt die dokumentierten Nachweise, auch für Raw die verifizierte
  eingefrorene Daily-Basis. Frühere Chunks ohne diese Policy werden nicht
  als verifiziert fortgesetzt. Legacy-Replay ohne neue Speicherbasisfelder
  bleibt unter seinem bisherigen nicht PIT-zertifizierten Vertrag lesbar.
- Nach einem Strukturfehler werden spätere Beobachtungen konservativ nicht
  zugelassen. Dadurch kann spätere Abdeckung verloren gehen; frühere gültige
  Ergebnisse werden nicht anhand dieser späteren Fehler neu ausgewählt.

Das ZIP enthält den vollständigen Diff gegen den unveränderten Basis-Commit,
einschließlich aller 20 neuen Dateien, diesen Abschlussbericht, technische
Dokumentation, Vorher-/Nachher-Regressionen, vollständiges Offline-Testprotokoll,
Offline-Runner, Dateimanifest und SHA256-Prüfsummen. Große Caches, Secret-Dateien
und heruntergeladene Massendaten sind ausgeschlossen.

Der Patch wird separat auf einer exportierten sauberen Kopie des Basisstands
angewandt und bytegenau gegen den Workspace geprüft. Der echte Arbeitsstand,
Index und HEAD werden dabei nicht zurückgesetzt oder neu aufgesetzt. Kein
Commit und kein Push. Nach Bereitstellung des Pakets wird auf Freigabe gewartet.
