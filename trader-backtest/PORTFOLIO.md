# Event portfolios and empirical Monte Carlo

For the rehydrated full universe and the separate equal-weight
`EVENT_RETURN_PORTFOLIO` study, see [EVENT-PORTFOLIO.md](EVENT-PORTFOLIO.md).
The true-trade minimum sample policy supersedes the earlier pilot: fewer than
30 trades is `VERY_LOW_SAMPLE` and cannot produce a strategy Monte Carlo;
30-99 is `LOW_SAMPLE`; 100 or more is regular exploratory simulation. The five
old Minervini trades and their archived resamples are technical function tests
only. Daily event return books never use a stop-based risk fraction.

This extension leaves the original daily, event, entry, exit and risk sources
and all strategy parameters unchanged. It does not optimize strategies, place
orders, contact a broker, commit or push. The main analysis never applies a
prop-firm challenge. All numerical analysis is offline.

## Configuration and frozen comparisons

`portfolio-config.json` declares EUR 10,000, no leverage, risks 0.25%, 0.50%
and 1.00%, five simultaneous positions, 3% simultaneous planned stop risk,
REALISTIC costs, seed 20261111 and 10,000 paths per available trade scenario
and bootstrap method. COOLDOWN20 is primary; FULL_RESET is a separate sensitivity.
Both call the existing event extractor, with warmup before the study boundary.
Continuous active setups cannot create repeated trades or periodic reentries.

Other capitals EUR 5,000/25,000/50,000, ten slots, aggregate risk 2%/5%, and
IDEAL/REALISTIC/STRESS are explicit configuration/CLI scenarios. Optional risks
1.50% and 2.00% are flagged `aggressive=true`; they are not in the baseline.
The lists of alternative scenarios are a catalog, not an automatically executed
Cartesian search or optimization. Choose a separate output directory for each
configuration. Existing absolute USD risk guards still affect capital scaling.

Candidates compete by trigger/setup status, RS_PROXY, liquidity, symbol and ID.
This order is fixed before outcomes. A qualifying trigger ranks above a ready
setup; in the executable cohort all rows already require a confirmed trigger.
No quality/outcome filter refills a rejected event, changes a date, or ranks
based on subsequent returns.

## Events are not orders or complete trades

`portfolio_sources.py` can read the original frozen event matrices with
`--analysis` and `--events`. For a small fresh dataset it calls the unchanged
native scanners over the common complete 260-session cohort, retains warmup
states, and calls `event_signals.extract_events`. Native percentile ranks are
computed before selecting events. The eight-stock pilot has a different rank
population from the absent 5,933-stock study and cannot reproduce its signals.

The execution rule is predeclared: take only the first **confirmed subsequent**
native trigger belonging to an accepted event, before a full reset, unknown
state, or newer accepted event. Minervini uses its existing ORB entry plan;
Qullamaggie and Confluence use their own existing confirmed trigger/stop. This
is a measured execution overlay, not a claim that a Trend Template PASS itself
defines an entry. A daily-close event cannot authorize an earlier intraday fill.

All intraday bars from the session after the event through the trigger must
exist without holes. Otherwise the first trigger might have occurred in the
unobserved interval; the first later observed trigger is not substituted.
An event is consumed once a complete plan is linked, even if risk/slots later
reject it. No later, more profitable opportunity replaces that plan.

The original `config.first.json` execution policy is retained: next bar open,
native known stop, existing exit planner's 2R target, one session, flat by 15:55
ET, shifted to 12:55 on early closes. The existing `engine.exit_price` handles
gaps at the adverse open and resolves ambiguous stop/target bars to the stop.
The bar-end timestamp is retained for these exits, conservatively keeping a
slot occupied through the bar. Every bar through the actual exit must exist.
Missing exit histories are recorded, not replaced with assumed stops or targets.
The native daily/intraday source replay still requires its supported calendar.
Multiday source reconstruction requires a separately specified exit policy and
is rejected rather than invented; the portfolio book can consume complete
multiday empirical path templates.

Missing plans receive `NO_COMPLETE_POST_EVENT_TRIGGER_PLAN`; failed price paths
have their exact reasons in `event-trade-coverage.json`. A scenario without
trades reports unavailable wealth/risk statistics, not a successful cash-only
portfolio. All such events remain in a separate gross forward-return table.
The reported executable cohort is conditional on reconstructible history, not
an unbiased historical opportunity census.

## Capital, costs and trader-risk

For a USD mid-price quote and USD-per-EUR FX rate:

```
risk/unit = (adverse entry-to-stop distance + full round-trip costs) / FX
quantity <= floor(account equity * risk fraction / risk per unit)
```

Whole-share sizing also applies remaining aggregate stop risk, available cash,
cash buffer, order and position caps. The aggregate admission cap includes the
equity reduction from entry friction/fees. Long principal leaves cash at entry;
exit proceeds return cash. Every mark and fill converts through explicit FX.
No long position borrows capital. Duplicate active tickers are rejected.

The shared `trader-risk/exposure.terms` supplies the cost convention. The
original `trader-risk/check.DEFAULTS` and `window_check` supply the blocked list,
cash buffer, position/order minima and maxima, daily total stop-risk budget,
daily trade/order counts, entry window, short/margin permission and minimum
net reward/risk. The user's percentage-based per-trade rule replaces the fixed
USD 50 per-trade ceiling for these simulations. Defaults remain USD 4,000 per
order, USD 150 daily cumulative stop risk, three trades/day, ten orders/day,
75% position cap and 10% cash buffer; all applied values can be reviewed in the
source and frozen configuration. `risk_rules` exposes supported explicit
overrides; unsupported live-account/broker guards are not purported to run.
Gaps, price losses and exchange-rate moves can breach planned risk after entry.
There is no claim that a stop guarantees the budgeted loss.

IDEAL removes fees/spread/slippage. REALISTIC uses the existing pilot's
10 bps full spread, 5 bps slippage per side, USD 0.01/share round-trip fee.
STRESS doubles slippage and widens spread by the existing multiplier.
These are **scenario allowances**, not observed historical costs. Unknown
historical spread, market impact, borrow rates or financing are never inferred.
Short P&L is supported for separately supplied verified borrow availability and
explicit nonnegative round-trip borrow cost, with 150% collateral and explicit
short/margin permission. The primary stock-long comparison needs neither.

EUR accounts require the archived `EURUSD=X` daily series. Its UTC daily close
becomes available 24 hours after its timestamp, avoiding an unfinished daily
FX candle. Carry of the last completed quote is limited to four calendar days;
otherwise reconstruction fails with missing/stale FX. This is a daily valuation
proxy, not observed intraday FX or an FX conversion-fee model. USD accounts can
be run explicitly; missing FX is never treated as EUR=USD.

## Statistics and resampling

The book records net trade P&L in account currency, realized R against initial
planned risk, holding time, admissions, rejections, bar-marked equity and daily
equity. Metrics include absolute and percentage gains, calendar-time CAGR,
daily max/average drawdown (average includes zero-depth sessions), longest
drawdown in sessions including an unrecovered terminal spell, intraday marked
drawdown, win rate, profit factor, expectancy, winner/loser averages, medians,
R statistics and consecutive wins/losses. Average position count and exposure
are time weighted in exchange trading minutes, including idle sessions. Maximum
positions counts intraday holdings, including positions flat at the close.

Sharpe/Sortino use EUR daily marked returns, 252 sessions/year, zero risk-free
rate/MAR and at least 60 observations. Undefined ratios, zero-loss profit
factors and empty cohorts remain null. CAGR over a short pilot is descriptive.
Intraday drawdowns use actual observed bar marks, not unknown intrabar paths.

Monte Carlo does not fit a normal distribution or sample a mean return. It
resamples **complete observed admitted trade paths** including entry/stop,
notional, duration, marks, exit and FX. Each synthetic path recomputes position
sizes with the chosen equity-risk fraction and reapplies cash, aggregate risk,
duplicate ticker, slot, window and daily risk limits.

- TRADE_IID draws complete trade bundles with replacement and moves them to
  the observed entry schedule. Durations are preserved in trading minutes;
  new conflicts/rejections are possible. Cross-trade regime dependence is lost.
- SESSION_BLOCK uses noncircular moving blocks of 20 consecutive **exchange
  sessions**, including zero-trade sessions. All trades originating in a session
  travel together; their full paths can extend beyond a block join. This
  retains within-block market clusters. It does not preserve dependence at
  joins or claim to reconstruct a new coherent market. Source-session phase is
  scaled to destination-session duration for exceptional early closes.

Blocks need at least two full blocks in the time axis; otherwise they are
explicitly unavailable. Trades open beyond the simulated horizon keep their
last observed empirical mark, with an open-position flag; no fictitious close
is inserted. Realized streaks exclude unclosed trades. Simultaneous exits use
deterministic admission/ranking order. Quote and FX bundles travel together;
FX is not a jointly simulated currency process across independently drawn
trades. Results are conditional on the observed accepted trade distribution,
not an unconditional new strategy backtest or a signal-frequency forecast.

Every available method reports mean/median terminal capital, P01/P05/P10/P25/
P50/P75/P90/P95/P99 of capital, net gains and percentage gains, intraday/daily
drawdown quantiles, probabilities of terminal loss, DD >5/10/20/30%, capital
depletion and gains >=25/50/100%, and worst/median/P95 maximum losing streak.
Shared seeds pair resampling across risk scenarios. Zero hits in 10,000 paths
are a sample frequency, not proof that an outcome is impossible.

The separate forward bootstrap draws the actual gross 20-session event
outcomes with replacement, or whole-session clusters in blocks at least as long
as the forward horizon. It reports the distribution of the **mean event return**,
never EUR wealth, stop-based R or risk-specific portfolio probabilities. Empty
block resamples are counted; displayed mean quantiles condition on nonempty
resamples. It does not turn three observed events into 10,000 independent ones.

## Data quality and provenance

With `--raw-daily`, `portfolio_quality.py` reuses the existing uniform
`data_quality.classify` protocol over all original raw daily quotes. Event QC
includes the original 260-session feature window and subsequent 20-session
forward window. ALL remains the primary panel; VALID_ONLY forward statistics
are a labelled retrospective sensitivity. No quality flag repairs a price,
changes ranks, creates a trade, refills slots or deletes inconvenient outcomes.
Without raw archives, `RAW_SOURCE_QUALITY_NOT_RECHECKED` is explicit.

Frozen protocol and dataset/FX/source hashes, source events, native replay,
coverage, costs, failures and per-scenario curves remain auditable. Caches must
match the dataset, native configuration and unchanged strategy source hashes.
The original backtest modules/configs have a separate source-hash regression
fixture pinned to origin/main e1edb15.

Always retain SURVIVORSHIP_BIAS_RISK, missing historical security identities,
missing delisted listings, revisable vendor prices and no true point-in-time
history. A new eight-ticker pilot is not the previous expanded universe study.
No winner is forced when the samples cannot support the comparison.

## Reproduction

Python 3.11 with the already tested NumPy/Pandas versions in
`requirements-daily.txt`; the underlying scripts retain Python 3.8 syntax.
Historical run artifacts are ignored, not distributed in Git. A fresh vendor
acquisition can differ from earlier snapshots. Existing frozen inputs must not
be overwritten with a new configuration.

```bash
# Explicit network acquisition for a new pilot directory only:
python3 trader-backtest/portfolio_collect.py \
  --output trader-backtest/runs/portfolio-new

# Entirely offline analysis after acquisition:
python3 trader-backtest/portfolio_run.py \
  --dataset trader-backtest/runs/portfolio-new/data/dataset.json \
  --fx-raw 'trader-backtest/runs/portfolio-new/fx/raw/EURUSD=X-1d.json' \
  --raw-daily trader-backtest/runs/portfolio-new/data/raw \
  --output trader-backtest/runs/portfolio-new/results

# Same fixed rules, different explicitly chosen portfolio scenario:
python3 trader-backtest/portfolio_run.py \
  --dataset trader-backtest/runs/portfolio-new/data/dataset.json \
  --fx-raw 'trader-backtest/runs/portfolio-new/fx/raw/EURUSD=X-1d.json' \
  --raw-daily trader-backtest/runs/portfolio-new/data/raw \
  --cache trader-backtest/runs/portfolio-new/results \
  --capital 25000 --max-positions 10 --max-portfolio-risk 0.05 \
  --cost-scenarios IDEAL REALISTIC STRESS --include-aggressive \
  --output trader-backtest/runs/portfolio-alternative/results

python3 -m unittest discover -s tests -q
```

First run in this workspace: `runs/portfolio-first/results/REPORT.md`, with raw
quotes under `runs/portfolio-first/data/raw` and FX under
`runs/portfolio-first/fx/raw`. Daily event study: 2026-01-02 through 2026-11-06;
common intraday coverage: 2026-08-18 through 2026-11-06 (58 sessions). The source
event/replay cache is under `runs/portfolio-first`; pass it via `--cache` to
repeat this exact frozen experiment without re-evaluating all native bars.

## Generic challenge extension

`portfolio_challenge.evaluate_challenge` supports profit target, maximum total
loss, maximum daily loss from supplied day-opening equity, optional trailing
peak drawdown, minimum trading days, maximum risk per entry and a calendar-day
time limit. No prop firm or default target is hard coded. Pass ordered marked
equity observations and each day's opening observation; a maximum-entry-risk
rule requires the entry risk. Loss takes precedence on simultaneous barrier
hits; bar observations alone cannot prove intrabar target/loss ordering.

`first_hit` returns an actual first hit index **or None**. Never-hit barriers
cannot be mistaken for index zero. Tests cover no hit, an actual index-zero hit,
target first, loss first, both hit at different times, simultaneous hits, daily
and trailing rules, day counts, deadlines and risk breaches. The main analysis
deliberately rejects non-null `prop_challenge` configuration: run that future
analysis separately.
