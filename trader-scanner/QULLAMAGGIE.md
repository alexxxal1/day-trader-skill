# Independent Qullamaggie research modes

These are systematic approximations to Kristjan Kullamagi / Qullamaggie's
published discretionary setups, not a reproduction of his judgment or returns.
They do not invoke the Minervini Trend Template. Shared OHLC normalization,
exchange clock and ordinal-percentile utilities do not combine the filters.
All existing modes and default watchlist/Minervini files are preserved.

```bash
python3 trader-scanner/scan.py universe --out /tmp/q-universe.txt
python3 trader-scanner/scan.py qullamaggie-breakout --universe /tmp/q-universe.txt
python3 trader-scanner/scan.py qullamaggie-ep --universe /tmp/q-universe.txt \
  --evidence /tmp/checked-catalysts.json
python3 trader-scanner/scan.py qullamaggie-parabolic --universe /tmp/q-universe.txt \
  --variant both --evidence /tmp/point-in-time-metadata.json
```

Each mode defaults to its own `trader/qullamaggie-*.json`. `--no-write` prevents
all report writes. There is no order, broker, sizing, risk-state write or
automatic exit. `LONG`/`SHORT` describe a confirmed structural direction only;
`signal: null`, `trade_allowed: false`, `paper: true` and `analysis_only: true`
always remain. `WATCH` covers untriggered candidates and risk/stop warnings;
`NO_SETUP` covers failed or insufficient observations.

## Data, rank population and replay

One year of Yahoo daily OHLC is fetched for **every** member of `--universe`
(default `trader/universe.txt`), with bounded workers and the existing host
fallback/retry. At least 127 contiguous completed sessions are required. Missing
latest session, invalid prices, missing sessions or unknown split basis yield
`INSUFFICIENT_DATA`. Only 2026-27 have a verified exchange calendar.

1M/3M/6M mean 21/63/126 completed trading intervals. Returns use split-adjusted
close prices, without dividend adjustment. Percentiles are
`100 * (average ordinal rank - 1) / (N - 1)`; ties share their mean rank and
fewer than two eligible stocks means unavailable ranks. This is a relative
price-performance proxy, not a proprietary rating. Every rank is in `ranks`.
`--symbols`, `--watchlist`, `--limit` and liquidity floors select results **after**
ranking, so narrowing the report never changes its reference population.
The requested/fetched/eligible counts and per-period ranked counts expose
partial coverage. Exclusions can change ranks.

During regular hours, current 1m OHLC is fetched for up to `--max-intraday 200`
selected symbols in universe order. Use `--max-intraday 0` for all selected
symbols. The skipped count is explicit. Outside regular hours, the default
live scan uses completed daily history, labels `PRE_MARKET_LAST_SESSION` or
`LAST_SESSION`, and emits no current intraday trigger. It does not claim to
observe a current pre-market gap. Supply `--intraday` for saved 1m/5m data;
pre/post-market bars appear only as separate volume/price context.

```bash
python3 trader-scanner/scan.py qullamaggie-breakout --universe /tmp/q-universe.txt \
  --save-data /tmp/q-snapshot --out /tmp/q-breakout.json
python3 trader-scanner/scan.py qullamaggie-ep --offline /tmp/q-snapshot/1d \
  --universe /tmp/q-universe.txt --intraday /tmp/q-snapshot \
  --now '2026-11-09 10:00' --evidence /tmp/checked-catalysts.json --no-write --json
```

Saved raw responses live under `1d/` and `1m/`. `--intraday` also accepts a
single supported Yahoo JSON bundle or directories `1m/` and `5m/`. Intraday
interval is read from `meta.dataGranularity`, never inferred from filenames.
A 5m candle cannot establish a 1m OR. Complete 1m candles can form 5m candles.
Every regular-session slot from the open through the clock must be present;
a missing/invalid slot disables confirmation. Only bar closes confirm range
breaks; crossing wicks alone do not. 60m OR confirmation requires a completed
post-range candle. A trigger's LoD/HoD is frozen at that candle, not recomputed
using later prices. A later stop breach invalidates the observed trigger.

Future bars and incomplete daily candles never establish intraday evidence.
Split snapshots are rebased to actions effective at the scan clock, including
splits effective this morning. The vendor may revise data; these checks cannot
reconstruct unprovided historical vendor vintages, delisted listings or a
point-in-time universe. Historical ranks need a historical listing universe;
the present Nasdaq directory has survivorship bias if reused for old dates.

## Measured rules and explicit heuristics

| Setup | Mandatory proxy | Quality/context, not an absolute rule |
| --- | --- | --- |
| Breakout | >=98th percentile in at least one period; ordered trough-to-peak impulse >=30% within up to 63 prior sessions, peak in final five; contracting 10-40 session base after that impulse | Multiple leader periods; higher second-half low; proximity to rising SMA10/20; SMA50 distance; ATR contraction |
| EP | Open >=110% of preceding close; volume >=2x prior ADV20, or >=1x prior ADV20 within first 30 minutes; explicitly confirmed catalyst | Prior 63/126-session moves penalize quality above 50%; supplied earnings growth/beat/guidance; extended-hours volume |
| Parabolic short | Large cap >=$10B: >=50%; small cap <$2B: >=300%; unknown/mid cap: >=100%, over 20 sessions; plus >=3 consecutive >=3% days or accelerating trend | Cap, liquidity, streak, acceleration, 10/20-day mean-reversion zones |
| Parabolic long | A proven prior parabolic structure followed by a completed breakdown and >=50% decline from a recent peak within 10 sessions | Same independent trigger requirements; no assumed guaranteed reversal |

All thresholds are configurable with `--setup-config`; see
[qullamaggie.example.json](qullamaggie.example.json). No upper impulse/extension
cap excludes 100%, 300% or 1000% moves. Unknown market cap is explicit, never
silently treated as a large cap. Metadata requires point-in-time timestamps and a source reference. A cap
snapshot available only after an observed range break cannot classify that
earlier trigger.
The 50% long threshold includes deeper 60%+ declines.

Breakout base range = high-low of the second half divided by the first half;
ADR contraction uses mean high-low ranges in the same halves. Both must be
<=0.85. Higher lows compare half-base minimum lows. ATR is mean 14-bar true
range (not Wilder ATR); half-base ATR uses the available within-half changes.
ADR is a mean 20-session dollar range; `adr_pct` is mean high/low percent range.
The impulse's peak must also be the highest high in its prior window, preventing
an old peak from relabelling the tail of an overlong base. These definitions
are measurable approximations to discretionary chart structure. The strongest
contracting valid base is selected deterministically; duration is trading
sessions. `setup_quality` is a checklist score, not a probability. Breakout awards
20 points each for a leader period, multiple leader periods, a valid base,
higher lows and MA support. EP starts at 35, adds 25 each for volume and a
confirmed catalyst, 5 without a prior large move, and 2.5 each for supplied
positive EPS/revenue growth, analyst beat and raised guidance (at most 100).
Missing earnings numbers award no points and are never inferred. A matched
parabolic short/long proxy scores 80/75 respectively.

A completed daily close above the previous base high is reported under
`daily_breakout` with the actual session-close availability time. It is daily
context, not a reconstructed intraday entry. Current breakout/EP confirmation
uses 1m/5m/60m ORH (and for breakouts also requires price above base high).
Parabolic shorts support ORL, first red **completed 5m** candle after further
extension, and an ordered crack -> bounce near VWAP -> red close below VWAP.
VWAP is a cumulative typical-price/volume proxy. A subsequent close reclaiming
dynamic VWAP invalidates its failure signal. Long reversals support ORH/range
break and the first green completed 5m candle after the multi-day collapse,
including the opening candle without requiring another morning low.
`observed_triggers` retains all supported structural observations; `trigger`
selects the earliest independently eligible one.

## Catalyst evidence and existing skills

Pass `--evidence FILE` repeatedly to merge native `trader-news --json`,
`trader-social` and `trader-quiver` reports plus explicit normalized events.
Native headlines/posts/disclosure trades are **supporting leads**, never
silently promoted into a confirmed cause for a gap. In particular, a Congress
trade is not by itself a government catalyst. Missing news does not create an
explanation: an otherwise matching gap stays `CANDIDATE`.

A verified causal record follows
[qullamaggie-evidence.example.json](qullamaggie-evidence.example.json): symbol,
source_skill, category, actual published_at, available_at, confirmed=true and
source reference. Categories: earnings, guidance, government, regulation, fda,
biotech, contract, partnership, sector, macro, political. Accepted source skills
also include trader-sentiment, trader-entry, trader-exit, trader-budget and
trader-risk. Use these only for evidence they actually establish; an entry
check does not establish an earnings cause. Earnings numbers are optional and
only copied from the supplied verified record; absent numbers stay absent.

Both timestamps require an explicit timezone. Availability is the **later**
of publication and observation. Disclosure uses publication time, not trade
date. Date-only disclosures cannot establish intraday availability. Evidence
older than 96 hours relative to the event session is excluded. A catalyst
released after the first OR break cannot confirm that earlier trigger. Volume
must already meet its threshold at the selected trigger; later daily/session
volume cannot retroactively qualify it. Native reports fetched now cannot be
used as if observed yesterday. For historical replay supply archived evidence
with actual availability times. Do not put later analyst revisions or earnings
restatements into an earlier record.

## Stop/risk handoff and output

Breakout and EP stops use LoD, short stops HoD or VWAP reclaim. Proposed stops,
when intraday data exists but no trigger is confirmed, are separately labelled
`stop_context`; a missing LoD/HoD remains null. The existing
`trader-risk.scanner_stop_context` validates direction/numbers and exposes both
ATR/ADR multiples. Breakouts prefer <=1x either range; EP warns above 1.5x either
range (configurable). Excess distance gives WATCH. Short permissions default
to blocked unless allow_short and allow_margin are explicitly enabled in the
existing risk config. No Qullamaggie setting overrides these permissions or
hard account loss/size/daily limits. The full existing risk check remains
mandatory once an independent plan, quantity, target, costs, portfolio and
state exist. No synthetic profit target is supplied merely to pass risk.
SMA10/20 are context zones only and never automatic exits.

Every `results[]` row includes ticker, independent setup/variant, one of
CANDIDATE/SETUP_READY/TRIGGERED/FAILED/INSUFFICIENT_DATA, price, three ranks,
ADR/ATR, volume/RVOL basis, catalyst evidence, trigger time, stop time/basis,
quality, action and reasons. `hits[]` includes CANDIDATE, SETUP_READY and
TRIGGERED rows passing display liquidity floors. The full results retain
failures and unknowns. `--all-results` prints them; JSON always includes them.
Exit 0 when selected candidates exist, 1 for none or a reported input error.
