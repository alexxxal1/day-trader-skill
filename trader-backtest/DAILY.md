# Separate daily-history measurement

The expanded experiment is a daily signal-quality study. It does not need
intraday prices, the entry pipeline, a broker, synthetic trades or leverage.
The original eight-stock pilot, its data and all its reports are preserved locally.

## Saved experiment

All paths below describe local run artifacts, excluded from Git. A clone supplies
the acquisition and analysis tools, not the historical snapshots or generated
reports. Identical offline reproduction needs the original local raw archive;
fresh acquisition creates a new, revisable experiment. The tests use their own
small synthetic fixtures and need none of these saved run files.

- Diagnosis: `runs/coverage-diagnosis/DIAGNOSIS.md`.
- Full expanded report: `runs/daily-expanded/results/REPORT.md`.
- Frozen requests: `runs/daily-expanded/data/frozen-acquisition.json`.
- Acquisition ledger: `runs/daily-expanded/data/acquisition.json`.
- All raw responses: `runs/daily-expanded/data/raw-daily/*.json.gz`.
- UTC exchange calendar: `runs/daily-expanded/data/calendar.json`.
- Per-ticker reasons: `runs/daily-expanded/results/ticker-coverage-and-exclusions.csv`.
- Exclusion intervals: `runs/daily-expanded/analysis/daily-eligibility-intervals.csv`.
- Signal audit: `runs/daily-expanded/results/signal-ledger.csv.gz`.
- Full numeric results: `runs/daily-expanded/results/report.json`.
- Raw-data extremes: `runs/daily-expanded/results/extreme-returns-20d.json`.
- Source and matrix hashes: `runs/daily-expanded/results/verification.json`.

The current listing universe has 5,933 scanner-eligible US-listed equities
and ADRs. All were requested. Two daily requests failed, BAR was a non-equity
provider mapping, and 455 histories had no complete common 260-session window.
5,475 stocks were eligible on at least one date; the eligible set varies by date.
The frozen common period is 1993-01-29 through 2026-11-06, 8,502 sessions.
No data was silently refreshed after the interruption or at a later date.

## Method

Use the existing scanner's directory classifier, without new stock selections,
price floors, liquidity optimization or output limits. The original eight
tickers were a deliberately small project selection, not a data-source limit.

For a fair daily comparison, all variants use the same per-date population
with 260 complete consecutive sessions, including the current completed bar.
This is the existing Minervini history requirement. Native breakout can work
with 127 sessions; the comparison's common data cohort is explicitly stricter.
An IPO is never included before its history exists. A hole does not compress
time or get replaced by a later observation. Reasons and dates are auditable.

The native Minervini evaluator, Qullamaggie breakout function and confluence
classifier decide signals. Native percentile ranking is calculated before any
signal selection. The accelerator computes rolling input metrics, then calls
those unchanged functions. Near strict moving-average equalities it recomputes
the original sequential sums. Historical split price bases are restored.
Tests compare ordinary, constant-price and split scenarios to the originals;
daily_verify.py independently compares seeded real-data observations.

Five signal streams stay separate:

1. MINERVINI: Trend Template PASS.
2. BREAKOUT_CANDIDATE: exactly the native CANDIDATE status, no fabricated entry.
3. BREAKOUT_SETUP_READY: exactly the daily-determinable native readiness status.
4. CONFLUENCE_CANDIDATE: PASS plus CANDIDATE.
5. CONFLUENCE_SETUP_READY: PASS plus SETUP_READY.

One observation per ticker, date and status, anchored at the completed daily
close. Forward price returns after 1/3/5/10/20/60 exchange sessions use the same
calendar and period for every variant. Dividends are excluded for stocks and
SPY alike. This measures filter quality, not execution at a guaranteed close.
All variants and controls use gross returns and zero invented trading costs.
No signal dates, stops, targets or strategy parameters are optimized.

The earliest shared SPY close starts the study; earlier stock bars are warmup.
The saved XNYS schedule was generated with exchange_calendars 4.11.2, including
exceptional closures and pre-2007 DST. Numerical replay reads the saved calendar
and does not require that package or a live calendar endpoint.

IS/OOS uses the first 70% and last 30% of sessions. Crossing forward outcomes
are purged. Both parts use unchanged parameters; no fitting or optimization.
Because the universe is today's survivors, the temporal holdout remains a
retrospective-universe study, not a point-in-time validation claim.

## Random control

1,000 independent seeded sampling sequences. On each signal date, select
the same number of stocks uniformly without replacement from the same
contemporaneous data-eligible universe. Nested random subsets couple variants
while preserving each subset's uniform marginal distribution. Holding horizons,
dates, selected counts and cost assumptions match. Seed: 20261109.

Future completeness never determines ticker selection. Missing future prices
remain missing; available-case means follow the same policy as strategy means.
Selected counts match exactly. Observed sample counts can differ slightly and
their min/median/max are reported. No zero-return imputation or replacement draw.
These are signal controls, not executable portfolios with slots or reinvestment.

The report gives the median and P05/P95 of random *mean* returns and the share
beating the strategy mean. These are not p-values. Restartable checkpoints atomically
save the accumulators, cursor and exact RNG state together every 100 dates. A different configuration
cannot reuse the checkpoint. Bootstrap mean intervals resample whole signal
dates in moving blocks of the forward horizon, preserving cross-sectional groups.

## Reproduction

The core backtest tools use the standard library. This larger daily accelerator
uses the tested Python 3.11, NumPy/Pandas versions in requirements-daily.txt.

From the repository root, using the saved matrices:

```bash
python3 trader-backtest/daily_results.py all \
  --analysis trader-backtest/runs/daily-expanded/analysis \
  --output /tmp/daily-reproduction
```

To rebuild matrices and signals entirely offline from raw snapshots, choose a
new output directory with at least 4.5 GB free during the blocked transpose
(about 3 GB of final derived data):

```bash
python3 trader-backtest/daily_history.py all \
  --data trader-backtest/runs/daily-expanded/data \
  --output /tmp/daily-rebuilt-analysis --workers 2
python3 trader-backtest/daily_verify.py \
  --data trader-backtest/runs/daily-expanded/data \
  --analysis trader-backtest/runs/daily-expanded/analysis \
  --output /tmp/daily-native-equivalence.json
python3 -m unittest discover -s tests -q
```

daily_collect.py is a separate explicit network acquisition command; do not run
it to reproduce a frozen experiment. An interval=1d request with range=max can
return 3mo bars. Acquisition rejects the wrong granularity and requests explicit
dates. Raw response hashes and each failure are recorded.

## Interpretation and intraday isolation

Current listings exclude delisted stocks and do not reconstruct ticker/security
identity through reorganizations. Historical vendor prices are revisable snapshots,
not archived historical vintages. Flags include SURVIVORSHIP_BIAS_RISK,
REVISED_DATA_NOT_POINT_IN_TIME and NO_HISTORICAL_SECURITY_IDENTIFIER_ARCHIVE.
Millions of overlapping samples are not millions of independent trades.

Extremes such as PPCB's reported 0.15-to-2,500 move require corporate-action and
price-basis reconciliation. They are not silently corrected, deleted or treated
as validated profits. They can distort strategy and random means. Medians and
win rates help describe typical observations but do not solve survivorship bias.
The study does not establish a profitable edge or scientific validity.

The separate intraday inventory retains the existing eight-stock pilot and its
60 complete 5m sessions. The other 5,925 tickers were not collected for intraday
in this daily task; they are not asserted to be unavailable from the provider.
The saved Yahoo 422 response limits the tested 5m request to the last 60 days.
No daily result depends on an intraday file. No trigger is inferred from a daily
wick. No EP is confirmed without a historical catalyst archive. The old trigger
counts remain explicitly scoped to their original eight-stock ranking population.

No orders, broker calls, commits or pushes are performed by this workflow.
