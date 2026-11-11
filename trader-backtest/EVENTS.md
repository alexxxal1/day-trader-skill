# Event and data-quality hardening

The original daily observations and raw prices are preserved unchanged.
Native Minervini, Qullamaggie and confluence rules are not modified.
This layer postprocesses the saved states and evaluates predefined sensitivities.

Local report: `runs/events-hardened/REPORT.md`.
Local frozen protocol: `runs/events-hardened/frozen-methodology.json` and
`analysis-plan.json`, created before event-performance results were inspected.
The study retains the previous period, current-listing universe, source prices,
calendar, chronological 70/30 split and price-return conventions.

The source protocol `event-methodology.json` is versioned. Historical inputs,
frozen run copies, reports, quality evidence and checkpoints under `runs/` are
local artifacts and are not supplied by a Git clone. The offline commands below
require the original local experiment files. Tests generate small synthetic
fixtures without reading those artifacts.

## Event definitions

- Minervini activation: confirmed eligible FAIL -> PASS.
- Breakout activation: confirmed CANDIDATE/FAILED -> SETUP_READY.
- Confluence activation: no matching ready confluence -> SETUP_CONFLUENCE.

FULL_RESET accepts only the first activation after a full confirmed reset.
Minervini resets on FAIL. Breakout resets on FAILED, not a return to CANDIDATE.
Confluence resets on Minervini FAIL or Breakout FAILED; PASS+CANDIDATE does not
rearm a consumed ready episode. This deliberately differs from simple daily
READY toggling and is reported separately from the cooldown variant.

COOLDOWN20 accepts a confirmed new activation only if at least 20 exchange
sessions elapsed since the preceding accepted event for that ticker/strategy.
It does not emit another event after 20 days while the old state remains active.
A suppressed transition is discarded, not delayed until the cooldown expires.

Unknown eligibility is not a confirmed FAIL/reset. Activations after unknown
previous states are left-censored. Native warmup states before the shared SPY
study start are reconstructed to avoid artificial first-day activations.
State and cooldown persist through the IS/OOS boundary. Filters never create
new events, rearm episodes, refill dropped slots or choose better later dates.

## Dependence and forward windows

An event is deduplicated, not statistically independent. Count overlapping
windows separately at each 1/3/5/10/20/60-session horizon, by ticker/strategy.
A gap equal to the horizon shares an endpoint price but no holding interval.
COOLDOWN20 has no same-ticker 20-session overlap, but can have 60-session overlap.
Cross-sectional market dependence remains even with no same-ticker overlap.

The additional NONOVERLAP sensitivity greedily retains the first event and
waits at least the horizon before retaining another event for the same ticker.
This chronological choice occurs before quality/return inspection and continues
through the IS/OOS boundary. It does not resurrect an event when another is
subsequently censored or quality-filtered. Nonoverlap statistics are supplemental;
the random controls target the two full event streams, with their own spacing.

Bootstrap intervals use 1,000 moving-block resamples of whole signal dates,
block length max(20, horizon), retaining cross-sectional clusters. These do not
eliminate universe bias or prove temporal independence.

## Uniform data-quality checks

The detector scans the entire raw daily archive with the same rules, not a
manual list of bad tickers or losing/winning observations.

- INVALID: missing/nonfinite/nonpositive OHLC, contradictory OHLC ranges,
  negative/nonfinite volume, missing sessions, wrong currency/instrument, or
  confirmed mixed quote/adjusted-close units at a declared split.
- SUSPECT: close gross factor >=2 or <=0.5; a changed price with zero reported
  volume; 20 successive unchanged zero-volume sessions; unexplained
  adjustment-ratio changes by a factor above 1.25 or below 1/1.25;
  split discontinuities resembling raw share units;
  history before declared inception; source market/identity mismatch.
- VALID: no detected issue under those checks. This does not certify every
  corporate action, security identity or actual historical vendor vintage.

Zero volume alone, an ordinary correctly adjusted split and a long-horizon
large return alone are not INVALID. A forward gross factor >=10 or <=0.1 is
logged for inspection; it does not automatically alter its quality label.
Real extreme gap winners can be SUSPECT under the conservative daily jump rule.

Each event/horizon takes the maximum severity over its original 260-session
feature window and every bar through its forward endpoint. ALL includes every
arithmetically computable positive-price return, even finite suspect/invalid
basis observations. Undefined returns remain counted, not imputed. VALID_ONLY
is an explicitly retrospective sensitivity after fixed event extraction.
Original native RS ranking populations remain unchanged: bad peer histories
can still affect those ranks. This is not a new scan of a clean security master.

The PPCB case has independent evidence: the SEC 2025-02-14 10-Q reports a
1:60,000 reverse split effective 2025-01-29. Saved Yahoo metadata reports 1:2;
both quote and adjclose jump from 0.10 to 6,250. Generic rules flag the path;
no PPCB exception or repaired return is introduced. SEC evidence is audit-only,
not a backdated trading catalyst. Oasis/CHRD filings also document cancelled
legacy equity and a later merger. The historical identity archive is incomplete
for the full universe; a current ticker's continuous chart is not identity proof.

## Random controls

There are 1,000 replicates per variant/strategy/horizon, with matching dates,
selected event counts, contemporaneous eligible universe, horizons and QC rules.
Draws have no duplicate ticker per date and maintain at least 2 sessions between
same-ticker FULL_RESET control draws, or 20 for COOLDOWN20. They do not require
random tickers to pass the strategy, which would condition away the null control.

PRIMARY does not use future quality in ticker choice. Both ALL and VALID_ONLY
are evaluated afterward, so observed VALID counts can differ; selected counts
still match exactly and observed counts are fully reported.

VALID_MATCHED is a second, explicitly retrospective control: it matches the
remaining VALID event counts exactly for each date/horizon, sampling from the
same retrospectively quality-qualified universe. It is not a point-in-time
entry policy. Both controls are shown so count matching cannot silently become
future-informed trading selection. Infeasible replicates are disclosed; no
constraint is relaxed to manufacture a feasible or better random benchmark.

Uniform IID proposals are shared across horizons for computational efficiency.
For each horizon the accepted sequence remains uniform over its unlocked,
quality-allowed subset. Cross-horizon controls are paired, not falsely declared
independent. Final grouped results are separate from preserved checkpoints of
the slower initial engine. The global seed and all sampling rules are fixed.

Random Median/P05/P95 describe the distribution of simulated mean returns.
Strategy Median describes individual event returns. Empirical strategy percentile
uses half weight for ties. Zero better simulations out of 1,000 is not p=0 or
proof that the strategy must beat random choices in real trading.

## Distributions and interpretation

Every requested horizon and event IS/OOS panel reports P10/P25/P50/P75/P90/P95/
P99, fractions >+5%, >+10%, <-5%, <-10%, mean, median and win rate. The top 1%
contribution decomposes the unchanged arithmetic mean; it never removes winners
or defines a new optimized trading rule. Gross-positive-return concentration is
also reported so a large net-mean contribution is not confused with concentration
of all winning returns. Primary comparison horizon 20 was frozen in advance.

These are gross descriptive signal returns, not trade expectancy after costs.
Confluence event times can differ from standalone event times; the comparison
does not isolate a causal filter effect. The chronological holdout still uses
today's surviving universe and revisable prices, not a true historical PIT list.
Intraday stays separate. No orders, broker calls, commits or pushes.

## Offline commands

Use the already frozen daily archive/matrices and the same optional NumPy/Pandas
versions as DAILY.md. Preserve existing outputs; a new experiment needs a new
directory and an explicit separately frozen protocol.

```bash
python3 trader-backtest/event_signals.py \
  --analysis trader-backtest/runs/daily-expanded/analysis \
  --output trader-backtest/runs/events-hardened --workers 2
python3 trader-backtest/data_quality.py \
  --data trader-backtest/runs/daily-expanded/data \
  --analysis trader-backtest/runs/daily-expanded/analysis \
  --output trader-backtest/runs/events-hardened
python3 trader-backtest/event_statistics.py \
  --analysis trader-backtest/runs/daily-expanded/analysis \
  --output trader-backtest/runs/events-hardened
python3 trader-backtest/event_random_grouped.py \
  --analysis trader-backtest/runs/daily-expanded/analysis \
  --output trader-backtest/runs/events-hardened --workers 2
python3 trader-backtest/event_audit.py \
  --analysis trader-backtest/runs/daily-expanded/analysis \
  --output trader-backtest/runs/events-hardened
python3 trader-backtest/event_report.py \
  --analysis trader-backtest/runs/daily-expanded/analysis \
  --output trader-backtest/runs/events-hardened
python3 -m unittest discover -s tests -q
```

Checkpoint cursor, exact RNG state, spacing book and accumulators commit atomically.
Completed groups retain result accumulators and no longer need a spacing book.
No network request is made by these analysis commands. Independent SEC source
files are preserved under quality-evidence with their hashes in verification.json.
