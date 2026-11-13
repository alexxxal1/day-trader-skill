---
name: trader-backtest
description: Reproduce historical Minervini, Qullamaggie Breakout, Episodic Pivot, Parabolic Short and Long, and Breakout Confluence research with unchanged strategy functions, strict availability clocks, forward returns, simulated trades, portfolio limits, seeded random controls and explicit data-quality limitations. Offline analysis only; never places orders or contacts a broker.
---

# Historical strategy measurement

For actual historical TRIGGERED reconstructions, first follow
[TRUE-TRADES.md](TRUE-TRADES.md): bounded source audit, frozen acquisition,
native trigger prefixes, observed next-bar opens and trigger-time stops.
This distinct workflow uses PIT_SAFE/REALISTIC/conservative as primary and
requires at least 100 complete True Trades per strategy for strategy Monte
Carlo; lower samples are technical portfolio checks only. Do not substitute
the older event-return or pilot-bootstrap thresholds for that gate. No daily
fallback, catalyst backdating or new trailing rules. The first frozen
eight-stock result is in [TRUE-TRADES-FIRST-RUN.md](TRUE-TRADES-FIRST-RUN.md).

For event portfolios and empirical Monte Carlo, follow [PORTFOLIO.md](PORTFOLIO.md).
For full-universe Daily rehydration and equal-weight EVENT_RETURN_PORTFOLIO
measurement, use [EVENT-PORTFOLIO.md](EVENT-PORTFOLIO.md). Daily forward returns
cannot enter stop-based TRUE_TRADE_PORTFOLIO results. Fewer than 30 actual
trades is VERY_LOW_SAMPLE and no strategy Monte Carlo; old five-trade pilot
resamples are technical function tests only.
The event portfolio queues a completed Close-T signal for observed Open-T+1
execution and exits at Close-T+h, counting the entry session as session one.
Only existing cash funds entries; scheduled close proceeds are available at the
following open. Same-close event portfolio results are superseded. Run the
execution audit described in EVENT-PORTFOLIO.md for cash/exposure invariants,
nonoverlapping tickers, tail sensitivity and matched benchmark periods.
Use PIT_SAFE as the primary event quality methodology; ALL and legacy full-outcome
VALID are sensitivities. PIT_SAFE decisions use only the existing OHLCV checks
through signal close T, excluding later corporate-action/identity/SEC/vendor
metadata from admission. Keep SNAPSHOT_RECONSTRUCTION_NOT_POINT_IN_TIME_CERTIFIED
when archived original data vintages are absent. Never relabel old VALID
bootstrap outputs as PIT_SAFE. The quality-only audit and full bootstrap are
separate commands and scopes.
Use the existing source/event definitions unchanged; never fabricate a stop or
trade for a daily filter. Keep unavailable trade portfolios distinct from the
separate forward-event bootstrap. The generic challenge helper is prepared but
is not part of the main analysis.

Use this skill for historical comparisons, not parameter optimization.
Never claim that a backtest proves profitability. This is not financial advice.
The engine has no broker integration and creates no execution payloads.
Do not commit, push or trade unless the user separately requests those actions.

## Choose the data layer first

For new universe-wide strategy comparisons, use the separate Daily-History
workflow in [DAILY.md](DAILY.md). It never requires intraday files. Keep
CANDIDATE and daily SETUP_READY observations anchored at completed daily closes.
For methodological comparisons, add the frozen event/quality protocol in
[EVENTS.md](EVENTS.md): full resets and 20-session cooldown, ALL/VALID sensitivities,
overlap and nonoverlap cohorts, event-only IS/OOS, distributions and 1,000 matched
random controls. Deduplicated events are not automatically independent samples.
The original run.py workflow is retained to reproduce the archived pilot; its
mixed pilot signal stream must not populate a pure Daily-History result table.
Analyze TRIGGERED, EP and Parabolic intraday events separately using only actual
historical minute bars and available catalysts. Missing intraday files never
remove a stock from the daily study.

## Offline reproduction

Historical datasets and generated reports are local artifacts, excluded from
Git under `runs/`. A clone includes no historical snapshot. The tests generate
small synthetic fixtures and do not depend on these run artifacts.

For the original pilot, with its original local dataset already available,
from the repository root, Python 3.8 or later, standard library only:

```bash
python3 trader-backtest/run.py \
  --dataset trader-backtest/runs/first/data/dataset.json \
  --config trader-backtest/config.first.json \
  --output /tmp/day-trader-backtest-reproduction
python3 -m unittest discover -s tests -q
```

The locally saved first run is in `trader-backtest/runs/first/results/REPORT.md`.
The expanded daily study is separate: see [DAILY.md](DAILY.md) and
the local `trader-backtest/runs/daily-expanded/results/REPORT.md`. It uses 5,933 requested
stocks, 5,475 data-eligible stocks over time and the common SPY-backed period
1993-01-29 through 2026-11-06. This accelerator has optional NumPy/Pandas
dependencies; the core tools and pilot remain standard-library only.
The configuration is frozen before acquisition and before observing outcomes.
Changing a configuration creates a new experiment; do not overwrite prior runs
to hide unfavorable results. Bug fixes must be documented separately from rule changes.

## Acquisition is separate

```bash
python3 trader-backtest/collect.py \
  --config trader-backtest/config.first.json \
  --output /tmp/new-historical-snapshot
```

Only explicit acquisition commands use the network: `collect.py`,
`daily_collect.py`, and the True Trade `intraday_audit.py` /
`true_trade_collect.py`. Offline replay never requests data.
Raw provider responses and their hashes are
retained. Existing cached responses are reused; replay never refreshes them.
Yahoo is unofficial, revisable, and offers only limited intraday history.
Do not infer historical news, social catalysts or disclosures from present reports.

## Dataset contract

`dataset.json` has `schema_version`, `sessions`, `universe`, `bars`, `splits`,
`evidence`, and `provenance`. SPY/QQQ belong in bars, outside the stock universe.
`sessions` maps ISO exchange dates to local New York `[open, close]` times.
The bundled calendar covers 2024-2027, including the 2025-01-09 closure.
Dates outside the supplied calendar are rejected; no weekday-only fallback.
The pilot's intraday decisions require 2026-2027, the existing entry/confluence
modules' verified calendar years. The separate Daily-History adapter uses its
saved historical UTC exchange calendar and does not depend on that entry calendar.
Bars are grouped as `bars.SYMBOL.1d`, `.5m`, or `.1m` arrays.
Each bar has `start`, `end`, `available_at` (precise UTC with timezone), and
`o`, `h`, `l`, `c`, `v`. Input prices use Yahoo split-adjusted OHLC, not adjclose.
Supply complete split events in `splits.SYMBOL` using the native Yahoo contract.
The existing normalizer restores the price basis known at the replay clock.
Trade simulation uses a consistent decision-time split basis throughout a position.

Multiple rows at one start time represent vintages. An as-of view chooses the
latest vintage available at that clock. Conflicting equal-time vintages fail.
Execution never substitutes a correction published after its bar ended.
Outcome statistics using latest vintages carry a separate revision flag.
Retrospective snapshots require `provenance.retrospective_snapshot: true` and
carry `REVISED_DATA_NOT_POINT_IN_TIME`. Their assumed bar-close availability
is not proof that a historical vendor delivered those values then.

Events need `published_at` and `available_at`. Native Quiver evidence also
requires `filing_at` or `disclosure_timestamp`. The latest of these and any
`revised_at` determines availability, never the transaction date.
EP additionally needs a verified categorized catalyst under the existing scanner rules.
Date-only or timezone-free events cannot establish intraday availability.
Historical market-cap observations belong in `evidence.metadata` with the same
publication/availability clocks. Missing caps retain the existing parabolic
UNKNOWN_OR_MID behavior; present-day market caps are never backdated.

## Strategy adapters

- `MINERVINI_FILTER`: completed-day Trend Template PASS; forward returns only.
- `MINERVINI_ENTRY`: prior completed PASS and the existing 15-minute ORB entry.
- `QULLAMAGGIE_BREAKOUT`: native SETUP_READY / TRIGGERED observations.
- `EPISODIC_PIVOT`: native ready/triggered states with contemporaneous catalyst.
- `PARABOLIC_SHORT`: actual native short trigger; extension alone never enters.
- `PARABOLIC_LONG`: independent native long trigger, separately reported.
- `CONFLUENCE_CANDIDATE`, `CONFLUENCE_SETUP_READY`, `CONFLUENCE_TRIGGERED`:
  native confluence classification with the completed Minervini PASS.

The adapters import existing scanner, entry, exit and confluence functions.
They never rewrite their criteria. Signal observations are deduplicated by
strategy, ticker, session and status. Repeated sessions remain dependent samples.
No CANDIDATE trade and no synthetic entry for a pure Minervini filter.

## Simulation policies fixed in advance

Confirmed trigger at a completed bar; entry at the next bar's open.
Missing next intraday slot means rejection, not a fill at a later better price.
Minervini ORB uses the existing opening-range-low stop and exit planner.
Qullamaggie retains its native trigger-time stop.
Targets use the existing exit planner's configured R rule, without claiming
these are proprietary strategy exit rules. The first experiment fixes 2R.
Fixed `flat_by` and `max_hold_sessions`; no retrospective selection of exits.
Stop gaps execute at the adverse open. A stop and target in one OHLC bar
resolve conservatively to the stop. Targets receive no favorable gap fill.

IDEAL has zero spread, slippage and commission. REALISTIC uses configured
full round-trip spread, slippage per side and round-trip commission per unit.
The existing trader-risk cost convention is reused. STRESS doubles slippage
and widens spread. These are cost assumptions, not historical spread observations.
Known historical short charges can be supplied under `borrow.SYMBOL` as rows
with `day`, `available_at`, `cost_per_share`. Missing data flags `BORROW_DATA_MISSING`.
Historical short locates/availability are not inferred; short results remain exploratory.
CFD plans are separately rejected with `CFD_NET_PERFORMANCE_UNAVAILABLE` until
an independent historical contract/quote/cost adapter exists. Stock quotes are
never relabeled CFDs; financing costs are never invented.

Equal weight allocates at most equity/max_positions per position.
Fixed risk allocates the configured equity fraction per initial stop plus costs.
Gross notional, portfolio risk, slots and ticker uniqueness constrain both.
Stock shorts additionally reserve 150% collateral, matching trader-risk's
stock-short convention; cash proceeds never become fresh buying power.
Leverage defaults to one; explicit higher leverage carries LEVERAGED_SCENARIO.
Ranking is fixed: confluence level, RS_PROXY, liquidity, symbol as tie breaker.
No future returns enter ranking or signal selection.

## Statistics and validation

Forward horizons are 1, 3, 5, 10, 20 and 60 exchange sessions.
Missing and right-censored outcomes remain in the audit, never zero-filled.
Mean, median, win/loss rate, standard deviation, MFE/MAE, SPY excess and
win-rate versus SPY are reported with sample sizes. Excursions start next session.
Intervals use a moving-block bootstrap of signal dates and are exploratory.
`LOW_SAMPLE_CONFIDENCE` applies below the predeclared sample threshold.
No confidence interval is produced with too few independent blocks.

Trade statistics include net P&L, expectancy, PF, holding time and per-trade R.
R uses initial planned stop risk including configured round-trip costs.
Portfolio equity marks active holdings; drawdown includes initial capital and
uses daily closes. Exposure is time-weighted gross notional/equity over sessions.
Sharpe/Sortino need 60 daily observations and a nonzero denominator; their
zero reference rate and 252-session annualization are explicit in the report.
Incomplete open positions invalidate net performance rather than inventing exits.

IS/OOS is chronological with crossing outcomes purged and identical start
capital per portfolio segment. The longer signal and shorter trade studies
have separately reported 70/30 dates. Walk-forward uses fixed train/test
windows without any parameter fitting. Insufficient history is explicit.

Random controls use the stored seed and at least the configured number of draws.
Signal controls match times, horizon, count and universe. Portfolio controls
match accepted trade counts and realized holding durations with random ticker
selection, identical sizing, costs, risk limits, slots and ticker uniqueness.
The portfolio control conditions on the observed schedule and uses timed exits,
not random reapplication of stops. Failed draws are reported, not replaced.
The fast matched portfolio control currently supports intraday trades; multi-
session trade controls explicitly report MULTI_SESSION_MATCHING_UNAVAILABLE.
Median, 5th/95th percentiles and fraction beating the strategy are descriptive,
not p-values or proof of an edge.

No delisted/PIT universe archive means `SURVIVORSHIP_BIAS_RISK`.
Report data span, gaps, samples, rejected trades, costs and unavailable tests.
Do not force a confluence conclusion from fewer trades or missing triggers.
Preserve input/configuration/source hashes and all individual observations.
