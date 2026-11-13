# Rehydrated universe and two separate portfolio layers

This workflow rebuilds the native daily and event infrastructure unchanged and
adds an equal-weight **EVENT_RETURN_PORTFOLIO**. It does not change strategy
parameters, choose a winning configuration, infer historical stops, issue orders,
contact brokers, commit or push. Run artifacts remain under ignored `runs/`.

## Frozen data and reproduction

`event_rehydrate.py acquire` saves the original scanner's Nasdaq directory
sources, native classification, hashes and 5,933-symbol requested list. It calls
the existing `daily_collect.collect`, with explicit daily granularity, full
available history and end date 2026-11-06. Every request/failure is retained.
The raw snapshot is immutable on replay. A newly acquired directory with the
same count does not prove identical membership to the missing old manifest.

Rebuild uses the existing `daily_history`, `daily_verify`, `event_signals`,
`data_quality` and `event_statistics` scripts, with unchanged full-reset and
20-session-cooldown rules, native ranks, split handling and QC. Checkpoints
freeze input/calendar/protocol/source hashes before those steps. The XNYS
calendar uses the original exchange_calendars 4.11.2, including pre-2007 DST
and exceptional closures. Native equivalence is independently checked.

The source config records the earlier expected 5,933 requested, 5,475 eligible
and Cooldown20 counts 129,177/37,964/18,876. These are comparison targets, not
numbers to manufacture by changing eligibility or strategy rules. Reports show
both total fixed events and horizon-specific complete VALID counts. Differences
can reflect directory membership, vendor revisions, availability, missing prices,
right censoring and QC definitions. Never silently match an old count by dropping
or adding tickers. PPCB is handled by the same generic detector as every ticker.

Dependencies are NumPy 2.3.5, Pandas 2.2.3 and exchange_calendars 4.11.2.
Numba 0.63.1 plus llvmlite 0.46.0 accelerates the cash/slot book without changing
its rules. Core event code retains Python 3.8 syntax; these tested accelerator
versions need an appropriate modern Python. An ordinary Python fallback supports
small numerical tests but is not practical for the full 10,000-path study.
`event-portfolio-requirements.txt` pins the additional packages.

```bash
# Explicit network acquisition, new local run directory:
python3 trader-backtest/event_rehydrate.py acquire \
  --output trader-backtest/runs/portfolio-expanded-20261111

# Offline native matrices, signals, uniform QC and event statistics:
python3 trader-backtest/event_rehydrate.py rebuild \
  --output trader-backtest/runs/portfolio-expanded-20261111 --workers 2

# Offline event-return portfolio and bootstrap; no intraday prerequisite:
NUMBA_NUM_THREADS=2 python3 trader-backtest/event_portfolio_run.py \
  --base trader-backtest/runs/portfolio-expanded-20261111 \
  --output trader-backtest/runs/portfolio-expanded-20261111/results-pit-safe

# Offline execution/accounting audit, tail diagnostics and matched benchmarks:
python3 trader-backtest/event_portfolio_audit.py \
  --base trader-backtest/runs/portfolio-expanded-20261111 \
  --corrected trader-backtest/runs/portfolio-expanded-20261111/results-next-open \
  --output trader-backtest/runs/portfolio-expanded-20261111/audit-next-open

# Quality-only comparison against the retained next-open ALL/VALID baseline:
NUMBA_NUM_THREADS=2 python3 trader-backtest/event_quality_analysis.py \
  --base trader-backtest/runs/portfolio-expanded-20261111 \
  --baseline trader-backtest/runs/portfolio-expanded-20261111/results-next-open \
  --output trader-backtest/runs/portfolio-expanded-20261111/quality-pit-audit

python3 -m unittest discover -s tests -q
```

Source config, source hashes, metadata, matrix content hashes, protocol and RNG
seed are frozen. Cached event pools and complete bootstrap output arrays permit
offline reproduction without fresh requests. A changed input/protocol needs a
different output directory. The native strategy files and original backtest
algorithms stay unchanged.

The completed local run also exports `reproduction-snapshot.tar`, its SHA256
checksum and a per-member manifest under the run directory. The bundle retains
the exact raw vendor responses, saved calendar/directory, frozen protocols,
result summaries and exact project sources including uncommitted additions.
It excludes Git metadata and large derived matrices, which can be rebuilt
offline. Preserve this bundle outside an ephemeral workspace when retaining the
original vendor vintage matters; fetching again cannot guarantee identical data.
After extraction, `source/` contains the tools and `run/` the frozen raw inputs.
Run the offline rebuild from `source/` with `--output ../run`, followed by the
event portfolio command using a new result directory. The included old result
protocol remains an audit reference and must not be overwritten with new paths.
That original bundle records the superseded same-close execution model. The
execution audit retains its original sources/results and a separate frozen
plan. Replay corrected results with the current source bundle and the original
raw snapshot; do not mistake the original bundle's close-return results for
causally executable fills.

## EVENT_RETURN_PORTFOLIO

The user selected USD 10,000 and the full available study starting with SPY in
1993. This avoids inventing EUR/USD before the euro existed. Each strategy has
its own book under identical capital, costs, horizon, quality and slot rules.
Primary horizon is 20 sessions; 1/3/5/10/60 are predefined sensitivities. Both
five and ten slots are run. No risk-per-trade percentage or R multiple exists
for this layer.

A completed native event observed at **close T** queues an entry at the actual
observed **open T+1**. Prices are normalized to that entry open, using mandatory
observed opens and closes on the same split-adjusted basis. The entry session
counts as session one: horizon h exits at **close T+h**, so a 20-session position
runs from open T+1 through close T+20. The old same-close model failed the causal
execution requirement and its results are superseded. There are no fabricated
triggers, stops, targets, inferred opens or intraday executions.
Undefined/incomplete paths are counted and excluded, never imputed as cash or
zero returns. Right-censored entries cannot be opened at the study boundary.
Dividends are excluded, as in the existing forward-return study.

Primary **quality methodology** is now PIT_SAFE: the existing numeric/OHLCV,
factor-of-two close jump, zero-volume changed-close and 20-session stale rules
applied only to the 260 sessions through signal close T. Corporate-action tables,
adjclose, current identity/inception metadata, SEC reconciliation and future bars
never determine that quality admission. Thresholds and strategy signals are
unchanged. INVALID_ONLY_PIT keeps SUSPECT and removes only the numeric/missing
past bars. ALL and the old VALID_ONLY remain sensitivity panels.

The old VALID_ONLY uses both the 260-session feature window and subsequent
outcome-window quality. It is retrospective and must not be presented as primary
historical strategy performance. Native event extraction and original ranks
occur first; QC never creates/rearms/reschedules an event or repairs a price.
The portfolio then competes among the requested quality cohort. The original
rank population, including any problematic peers, is not replaced by an
outcome-selected clean ranking universe. ALL is a separate 20-session portfolio
sensitivity with the same raw values and generic QC, including PPCB if its
outcome is arithmetically defined. No symbol-specific deletion occurs.

The causal rule implementation does **not** certify historical data vintages.
Existing raw OHLCV are a downloaded, potentially revised and split-adjusted
snapshot; archived original publications and historical security identities are
missing. PIT_SAFE output is explicitly
`SNAPSHOT_RECONSTRUCTION_NOT_POINT_IN_TIME_CERTIFIED`. The original listing,
signal/eligibility rules and completeness gates are not rebuilt into a PIT
universe by this filter. Missing/unvalued outcomes remain separately reported.
An archive adapter rejects missing publication timestamps and never selects a
revision published after the decision; it does not invent timestamps for the
current snapshot.

For competing events, rank by trigger/setup status, RS_PROXY, 20-session dollar
liquidity, and symbol. Within a separate strategy stream status is constant.
Retain the best 2*maximum_slots offers per date: at most maximum_slots unique
tickers can already be active, so this preserves every possible admission
without using its later return. All observed complete outcome paths remain
in the bootstrap pool, including those that lost the historical slot competition.

At each open, mark existing holdings using that session's observed opens.
Assign newly admitted events the batch's pre-entry equity divided by slot count,
capped at cash already available. The cash debit includes entry costs: five
slots target 20% per entry, ten slots target 10%; REALISTIC mid-value allocations
are 20%/1.001 and 10%/1.001 before a possible cash cap. Only actual cash funds
entries; unrealized gains are not credited as cash. Cash earns no interest.
Later at the close, mark holdings and settle scheduled expiries. Their proceeds
first become available for the following open, never the earlier same-day open.
Fractional index units avoid an artificial minimum whole-share effect. Initial
weights are equal, then drift; there is no daily rebalance or invented churn.
Only one active position per original ticker is allowed, with no borrowing or
negative funded cash. Returns and costs accrue in USD. Daily curves include
entry friction and the entire intermediate path, not just terminal outcomes.
The event index applies these declared capital/slot/identity constraints.
Intraday entry windows, stop-risk budgets and fixed-dollar broker ticket caps
require true execution plans and are not asserted for this return proxy. The
existing true-trade risk implementation and its rules remain unchanged.

IDEAL has zero modeled friction. REALISTIC applies the existing full spread
10 bps and slippage 5 bps per side: buy-value = cash allocated / 1.001;
exit proceeds = marked value * 0.999. These are allowances, not observed quotes.
The old USD 0.01/share commission is **not silently applied** to split-adjusted
fractional event index units. Historical actual share/contract fee terms are
unknown; the report says before unknown actual commissions/taxes. This is
the conservative methodological boundary of a Daily-only cost proxy, not an
assertion of a zero historical commission. IDEAL comparisons show the modeled
cost effect, including any resulting allocation differences.

Metrics include capital, percentage return, calendar-time CAGR, daily max and
average drawdown, longest underwater spell, net event-position win rate,
mean/median complete pool and admitted event returns, closed/admitted events,
time-average/max positions, marked exposure and modeled cost impact. Exposure
uses the post-entry open invested fraction across all sessions, including idle
sessions. Maximum exposure is checked after entries and before/after close
exits. The primary drawdown uses daily closes, including initial capital;
an additional open/close phase drawdown makes that sampling limitation visible.
Raw event counts, QC labels and price-window censoring are separate from admitted
portfolio positions. No result is labelled a true trade expectancy.

## Event Monte Carlo

For future full runs, at the primary 20-session PIT_SAFE horizon all three strategies and both slot
counts receive 10,000 IID paths and 10,000 moving-block paths, seed 20261111.
The identical compiled cash/identity/slot engine is used for historical books
and every bootstrap book. No normal distribution, fitted mean, fabricated stop,
leverage or risk-based size is introduced.

- IID_EVENT_PATH fixes the observed signal calendar, native ranking and offered
  ticker identities. On admission it draws an entire normalized historical
  outcome path with replacement from the full primary-quality event pool for that
  strategy/horizon, including nonadmitted historical candidates. It reruns cash
  allocation and duplicate/slot constraints. This destroys market dependence;
  Each sampled outcome includes its whole open/close trajectory. Sampled
  returns are an empirical outcome experiment, not new market prices
  or signals for that original ticker.
- SESSION_BLOCK samples 60 consecutive exchange sessions at a time, without
  circular wrapping. The ranked signal-date cross-section, actual event paths
  and inactive days travel together. All admission limits are reapplied.
  Full holding trajectories continue across a block join. Dependence at joins,
  regime persistence beyond the fixed block and a coherent new market universe
  are not claimed. Draws near the terminal study boundary are deterministically
  censored if their holding period cannot finish; no invented exit is inserted.

Per-path seeds are base_seed + path_index, enabling deterministic resume and
thread-count independence. Block draw calendars are paired across strategies
and slot scenarios, so comparison frequencies refer to the same sampled time
blocks. IID comparisons have no corresponding cross-strategy market pairing.
Blocks and IID can disagree; that discrepancy is reported, never tuned away.

The archived `results-next-open/` bootstraps use retrospective VALID and remain
labelled sensitivities; they are not relabelled as PIT_SAFE. The separate
quality-only audit does not rerun Monte Carlo or change the simulation scope.

Outputs include median/mean capital, P01/P05/P10/P25/P50/P75/P90/P95/P99,
terminal loss probability, daily maximum drawdown quantiles and probabilities
above 5/10/20/30%, and gain probabilities >=25/50/100%. Frequencies are
conditional empirical simulations, not unconditional future probabilities.

## TRUE_TRADE_PORTFOLIO and sample policy

A Daily SETUP_READY/PASS event supplies no historical trigger/stop/exit plan.
The rehydrated full universe is therefore INSUFFICIENT_TRADE_DATA for all three
true-trade portfolios until actual complete timestamped evidence is supplied.
Daily paths never enter the stop-based portfolio API.

`portfolio_monte_carlo.true_trade_sample_status` fixes these rules before results:

- 0-29 true trades: VERY_LOW_SAMPLE; no strategy Monte Carlo.
- 30-99: LOW_SAMPLE; explicitly small-sample exploratory Monte Carlo only.
- 100 or more: REGULAR_EXPLORATORY; data limitations still apply.

The previously reconstructed five small-universe Minervini trades are only a
TECHNICAL_FUNCTION_TEST. Unit tests can explicitly request `technical_test=True`
to test deterministic 10,000-path mechanics on small fixtures; the result is
labelled technical and this opt-in is never used by the main strategy report.
Archived pilot resamples do not establish strategy performance.

## Tail dependence and interpretation

The report attributes the unchanged event mean to its top 1% outcomes and the
unchanged portfolio P&L to its top 1% admitted positions. It reports medians and
shares of positive/net P&L; these are attribution diagnostics, not outcome-based
deletions or optimized strategy variants. Dollar P&L also reflects compounding
and changing allocation size. Native data-quality rules, corporate actions,
reverse-split, inception/ticker-reuse and jump checks are all retained.

`event_portfolio_audit.py` validates complete cash ledgers and nonoverlapping
ticker intervals, rechecks each Confluence top-20 P&L case against hashed raw
quotes and generic QC, and exports historical example days. It reports top-1,
top-10 and top-1% admitted-position P&L shares. Dollar shares can exceed 100%
of net P&L when other positions lose money.

Prespecified sensitivity diagnostics remove the top ceil(0.001*N) VALID event
returns or winsorize at P99.9/P99, with identical pool cutoffs for both slot
counts. Removal can change subsequent admissions while preserving native ranks.
Winsorization scales the complete cumulative open/close return index for affected
events by p'=1+(cap/r)*(p-1). These are outcome-conditioned synthetic index
diagnostics, never historical quotes, live selection rules or strategy tuning.

SPY and QQQ comparisons use price returns without dividends and the identical
BPS cost proxy. SPY covers the full study; QQQ is unavailable before 1999-03-10
in the frozen data. For QQQ, all strategy portfolios restart with equal capital
on exactly the common benchmark interval, retaining native event/cooldown states.

Reproducible arithmetic and observed within-snapshot comparisons can be checked.
No causal Confluence advantage, future profitability or true execution edge is
proved. Retain SURVIVORSHIP_BIAS_RISK, missing delisted memberships/historical
security identifiers, revised non-PIT prices, retrospective VALID selection,
dependent/overlapping outcomes and unknown historical actual costs.

## VALID selection audit

`event_quality_pit.rule_inventory()` lists every legacy flag, severity, data
dependency and information-clock classification. An OHLCV rule can be
POINT_IN_TIME_SAFE only when applied to genuinely available past/current bars;
using it to reject an event because of a later outcome bar is RETROSPECTIVE.
Current vendor inception/market/instrument metadata are UNKLAR without archives.
Split reconciliation and adjustment diagnostics use retrospective action/price
data. Declared reverse splits alone and the mere lack of an identity archive do
not automatically remove a ticker. SEC data do not enter the current QC filter.

`event_quality_analysis.py` freezes its analysis plan before performance results:
all four panels for all strategies and slots, each legacy rule removed alone,
cumulative removal in the existing ascending bit order, legacy feature-only
diagnostic, and top/bottom 20 Confluence P&L cases per panel/slot. Rule counts
overlap. ALL-book P&L attribution is not a counterfactual return contribution;
rerunning each mask changes admissions and compounding. Sequential capital deltas
telescope to the full ALL/VALID difference but are order dependent. No rule or
case is selected or removed based on performance.
