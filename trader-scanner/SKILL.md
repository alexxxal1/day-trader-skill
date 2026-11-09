---
name: trader-scanner
description: >-
  Chart Scanner. Scans thousands of charts for setups: opening-range
  breakouts, flags, oversold bounces. Builds the list of US-listed common
  stocks, ordinary shares and ADRs from Nasdaq Trader's symbol directories,
  runs a pre-market scan of daily closes (20-day closing-high breakout, bull
  flag, reclaimed the 20-day average, oversold, oversold bounce) and an
  intraday opening-range breakout scan on 5-minute bars, plus Minervini's
  Trend Template filter with an explicitly labelled RS_PROXY, then writes
  trader/watchlist.json for trader-news and trader-entry. Free Yahoo data,
  bar by bar. Use when the user says "scan the market", "find me setups",
  "what is breaking out", "opening range breakouts", "scan for bull flags",
  "oversold stocks", "pre-market scan", "build my watchlist", "what should I
  look at today", "Minervini", "Trend Template", "Stage 2 candidates",
  or at the start of a trading day before /trader-news.
argument-hint: "[universe | daily | orb | minervini | qullamaggie-breakout | qullamaggie-ep | qullamaggie-parabolic] [--at HH:MM]"
---

# trader-scanner

The Chart Scanner is the first step of a day trade. It reads thousands of
free charts and lists the ones that match a rule. That is all it does. A scan
is a list of charts that match a rule, not a list of stocks to buy, and a
setup on a chart is not a prediction. Most people who day trade lose money;
the scanner does not change that. It only saves you from flipping through
5,000 charts by hand.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                       # -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                          # before the open -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols TSLA,NVDA,AMD  # or --watchlist FILE, --limit 200
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                            # after 9:50 AM ET -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:15                 # cut the clock at 10:15 AM ET
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist trader/watchlist.json --json
python3 "${CLAUDE_SKILL_DIR}/scan.py" minervini                    # full US universe -> trader/minervini.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" minervini --sort liquidity --show 20
```

Flags: `--min-price 5` and `--min-volume` (daily 1,000,000, orb 0),
`--or-minutes 15`, `--max-confirm 200` (orb stage 2),
`--now "2026-10-19 10:15"` (New York time) to pin the clock, `--offline PATH`
to read saved Yahoo files, `--no-write`, `--show N`, `--json`. Exit code 0
when the scan found something, 1 when it found nothing or could not run (the
reason is one plain line).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/universe.txt` | written by `universe`: one `SYMBOL\|Name` a line, read by `daily` and `orb` |
| `trader/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of`, `universe`, `scanned`, `failed`, `hits[]`. trader-news and trader-entry read `hits[].symbol` |
| `trader/minervini.json` | separate research report: all selected `results[]` with PASS/FAIL, PASS `hits[]`, all criteria, RS_PROXY methodology and population coverage; never an entry signal |
| `trader/config.json` | read by `orb`: `no_entries_after` (default 15:30) |

## The universe

`universe` downloads `nasdaqlisted.txt` and `otherlisted.txt` from
nasdaqtrader.com/dynamic/SymDir and keeps a row when, in this order:

1. It is not a test issue, not an ETF, not NextShares.
2. It is a plain ticker: 1 to 5 letters, or 1 to 4 letters plus one class
   letter after a dot (BRK.B). A `$` preferred (ABR$D) is out, and so is a
   `.U`, `.W` or `.R` suffix (units, warrants, rights).
3. Out, whatever else the name says: warrants; SPAC units and rights ("Units,
   each consisting of", "Rights to receive"); notes, ETNs, debentures; a %
   coupon; "Depositary Shares" that are not American or Global (those carry a
   preferred).
4. In: the name calls it common stock, common shares or units, ordinary
   shares, shares of beneficial interest, ADS / ADR / American or Global
   depositary shares, or partnership or trust units.
5. Out: the name still says unit, right, bond, preferred or preference.
6. In: everything else (plain names like "AMETEK, Inc.").

On the Oct 19 2026 files this keeps 5,943. The reel said 5,259: that count
came from an ad hoc filter (names that say common or ordinary, no ADRs). All
5,259 of those are in this list; the 684 extra are mostly ADRs, closed-end
funds and partnership units. The Oct 20 files gave 5,942.

## The daily scan (before the open)

Yahoo's spark endpoint returns closes for 20 symbols a request, so 5,942
symbols take 298 requests. On Oct 20 2026 that took 145 seconds with the
polite pause (0.3 s). Liquidity comes from the spark meta: last price at
least `--min-price` and last session's volume at least `--min-volume`. Run it
before the open or after the close: during the session the volume is the
day's so far, and today's daily bar is left out until 4:00 PM.

The setups are computed from daily closes only (spark has no highs or lows),
so they are close cousins of the reel's `refs/scan.py`, which used highs and
lows:

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

A closing-high breakout fires more often than an intraday-high one (KO on
Oct 16 broke its 20-day closing high, not its 20-day intraday high). Hits are
listed most traded first (price times volume).

## Minervini Trend Template (`/trader-scanner minervini`)

This is an additional filter for momentum / Stage-2 research candidates.
**PASS means all ten mandatory criteria match. It is never an automatic LONG
signal or an instruction to buy.** No order, sizing or broker connection.

First run `scan.py universe`, then `scan.py minervini`. The default downloads
two years of Yahoo daily v8 OHLC for the entire supported US-listed universe
defined above. It does not use the 3-month spark closes from `daily`.

| # | mandatory criterion |
| --- | --- |
| 1 | last completed daily close > SMA150 |
| 2 | close > SMA200 |
| 3 | SMA150 > SMA200 |
| 4 | SMA200 > its value 21 trading sessions earlier (`--trend-days 21`) |
| 5 | SMA50 > SMA150 |
| 6 | SMA50 > SMA200 |
| 7 | close > SMA50 |
| 8 | close >= 1.30 times the 52-week low |
| 9 | close >= 0.75 times the 52-week high |
| 10 | RS_PROXY >= 70, including exactly 70 |

The monthly SMA200 check compares two endpoints; it does not assert that
SMA200 rose every intervening day. SMA comparisons are strict. Annual price
thresholds and the rank threshold are inclusive. Display rounding is never
used to decide PASS.

### Data and time

- Price means the latest **completed daily close**, also during the session.
  All SMA, annual extrema, returns and liquidity use only bars completed by
  `--now`. Current quote metadata is never used as historical evidence.
- Yahoo historical quote OHLC is split adjusted, without dividend adjustment.
  `adjclose` (which also includes dividend adjustments) never silently replaces
  OHLC. Split events are retained. Explicit RAW input can be normalized by its
  supplied splits; already adjusted Yahoo OHLC is never adjusted twice.
  Saved snapshots with later splits are rebased to the historical clock.
- Require at least 260 valid daily bars (or `200 + trend_days`, if larger).
  Missing OHLC, invalid values, unknown adjustment basis or insufficient
  history produces `FAIL / INSUFFICIENT_DATA`. A missing latest completed
  session produces `FAIL / STALE_DATA`; it is excluded from the rank population.
- The 52-week high and low are the highest **high** and lowest **low** of the
  last 252 completed sessions, an explicit trading-year approximation. The
  12-month price return spans 252 completed trading intervals (253 closes).
  Histories spanning more than 380 calendar days for those intervals fail as
  sparse data. No live tick, incomplete daily bar or future date is included.
- Yahoo is unofficial and can revise history and corporate actions. These
  checks prevent future bars entering the calculation, but cannot reconstruct
  a historical vendor vintage or an unprovided point-in-time listing universe.
  The bundled exchange calendar covers holidays and early closes in 2026-27.

### RS_PROXY, explicitly not the original RS rating

The original criterion uses a proprietary cross-sectional RS rating. This tool
has no original IBD / Minervini RS rating. It labels its alternative `RS_PROXY`
everywhere: simple split-adjusted 12-month **price performance**, ranked across
all eligible members of the scanable US universe. It is not total return, an
RSI, an SPY comparison or a weighted blend of 3-, 6- and 12-month performance.

Sort returns ascending and compute
`100 * (average ordinal rank - 1) / (N - 1)`, with ordinal ranks starting at 1.
Ties share their average rank. The endpoints are 0 and 100; equal returns for
every member give 50. Fewer than two eligible stocks means unavailable RS_PROXY
and FAIL. Keep full numerical precision for the >= 70 decision.

`--symbols`, `--watchlist` and `--limit` select report rows **after** ranking;
they never shrink the live reference population. A fresh live run therefore
still downloads the full `--universe` or `trader/universe.txt` even when selecting
one ticker. Optional price/volume floors filter displayed PASS hits after
ranking, without changing the ten criteria or a stock's rank. Defaults impose
no additional liquidity floor. Sort PASS candidates by RS_PROXY then mean
20-session dollar volume (`--sort rs`, default), or dollar volume then RS_PROXY
(`--sort liquidity`). The ticker breaks any remaining ties deterministically.

The report lists requested, fetched, eligible and ranked counts separately.
Unavailable, stale or short-history stocks are excluded from ranking and fail;
reduced rank coverage is labelled PARTIAL. Their exclusion can change rankings.
Listings, ADRs, funds and partnership units follow the existing universe rules;
this is not IBD's population. IPO exclusion, missing/delisted securities,
universe selection and provider revisions limit comparability. No proprietary
rating, sector normalization or historical survivorship correction is implied.

### Output and handoff

Each `results[]` row contains ticker (`symbol`), `trend_template` PASS/FAIL,
`price`, `sma50`, `sma150`, `sma200`, previous SMA200 and `sma200_trend_pct`,
`above_52w_low_pct`, `below_52w_high_pct`, `RS_PROXY`, `fulfilled_criteria`,
`failed_criteria`, every boolean/unknown criterion and a failure reason.
Unavailable required values appear as `n/a` in text and null in JSON.
Text prints PASS hits by default; `--all-results` includes FAIL. JSON always
contains every selected row plus the PASS hits, and `--show N` only limits text.

The separate default `trader/minervini.json` preserves `trader/watchlist.json`.
`analysis_only: true`, `signal: null` and no `setups` tag keep the result a filter.
Use its `hits[].symbol` to research a selected chart with `/trader-news`,
`/trader-social`, `/trader-quiver`, `/trader-sentiment`, `/trader-entry` and
`/trader-risk`. Those skills must obtain and assess their own evidence; a
Minervini PASS does not confirm an entry, short or CFD contract.

```bash
python3 scan.py minervini --universe trader/universe.txt --show 10
python3 scan.py minervini --symbols AAPL,MSFT --json --no-write
python3 scan.py minervini --min-price 5 --min-volume 1000000 --sort liquidity
python3 scan.py minervini --save-data /tmp/minervini-charts --out /tmp/minervini.json
python3 scan.py minervini --offline /tmp/minervini-charts --universe trader/universe.txt \
  --now "2026-11-09 09:00" --all-results --no-write
```

`--workers 4` bounds concurrent v8 requests (1..8); `--pause 0.3` pauses each
worker between requests. Existing Yahoo host fallback and 429 retry apply.
`--save-data DIR` writes fresh raw Yahoo responses for replay; live scans never
silently read a cache. An offline scan without `--universe` ranks the entire
snapshot and labels that population explicitly. Include the universe file to
retain missing-symbol failures and exact coverage. Exit 0 if selected PASS hits
remain, 1 if none or the scan cannot run, as in the other modes.

## Independent Qullamaggie modes

`qullamaggie-breakout`, `qullamaggie-ep` and `qullamaggie-parabolic` produce
separate research reports, independently of Minervini. Read
[QULLAMAGGIE.md](QULLAMAGGIE.md) for exact rules, point-in-time evidence,
configuration, data requirements and replay commands. Breakout leaders expose
1M/3M/6M cross-sectional percentile ranks; EP requires a confirmed catalyst
and elevated volume; parabolic short and long remain separate variants.
A setup alone never triggers a trade. Without current completed intraday
bars, use CANDIDATE or SETUP_READY, never invent a trigger. Existing entry,
exit, budget and risk checks remain mandatory. No orders or broker calls.

## The opening-range breakout scan (during the session)

The opening range is the first `--or-minutes` (15) of the session: the 9:30,
9:35 and 9:40 five-minute bars. A hit is the first completed 5-minute bar
to CLOSE above the range high, at that bar's close time.

1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a
   request. A chart goes on only if a completed bar after the range closed at
   or above the highest close of the range. That is a necessary condition
   (the range high is at least every range close), so no breakout is lost.
2. Stage 2, each candidate: the v8 chart with real highs and lows, one
   request each. Range high = the highest high of the range bars, range low =
   the lowest low. On a strong morning most charts are candidates, so stage 2
   checks at most `--max-confirm` (200) of them, the most traded first (price
   times volume so far), and prints PARTIAL with the count it skipped.
   `--max-confirm 0` checks them all.

Timing, live on Tue Oct 20 2026 (a strong morning): 100 liquid names at
10:01 AM took 35 seconds (5 spark requests, 70 v8 charts, 51 of 91 usable
charts broke out). The whole universe at 10:03 AM took about 4 minutes (298
spark requests, then 200 v8 charts): 2,448 usable charts, 1,782 candidates,
the 200 most traded checked, 119 of those broke out. For a fast answer, scan
the daily watchlist or a list of names you trade (`--watchlist`, `--symbols`).

A chart is usable only when every 5-minute bar from 9:30 to the cutoff is
there; a missing bar means a halt or a thin stock. Null closes are dropped,
a bar off the 5-minute grid (the live tick, like 10:58:07) is dropped, and a
bar counts only once it is complete (start + 5 minutes). Before 9:50 AM, on a
weekend or holiday, after the close, or after `no_entries_after` (an expired
setup) it prints one plain line and writes nothing. The 1:00 PM early closes
are in the calendar.

## Data and limits

- Yahoo's chart and spark endpoints are free, unofficial and can lag. Bars are
  5-minute bars, bar by bar, never tick by tick. query2 first, query1 on a 429
  or 5xx, one retry after a 5-second wait on a 429.
- Partial results are said as partial: "Fetched 4,980 of 5,259 charts (279
  failed or had no data)". Never round a partial scan up to "the whole market".
- NYSE holidays and early closes for 2026 and 2027 (nyse.com, read 2026-10-13).

## Steps

1. Once a week or so: `scan.py universe`.
2. Before the open: `scan.py daily`. Tell the user the counts (fetched,
   liquid, scanned, hits) and the top few by setup.
3. After 9:50 AM ET (10:15 is the classic): `scan.py orb`. Tell the user how
   many usable charts broke their range and when, and that a later hit is
   closer to the price now.
4. The user picks one chart. Hand it on: `/trader-news TICKER` (what is moving
   it), then `/trader-entry TICKER` (the entry bar by bar). Never pick for them.

## Example: the reel

Monday Oct 19 2026, scan at 10:15 AM over the reel's 234-ticker wall
(`tests/fixtures/trader-scanner/wall5m-2026-10-19.json.gz`, real Yahoo charts):

```
python3 scan.py orb --offline wall5m-2026-10-19.json.gz --now "2026-10-19 10:15" --min-price 0
Fetched 233 of 234 charts (1 failed or had no data); 231 usable; 95 candidates, 95 confirmed with real highs and lows.
42 of 231 charts closed above their opening-range high by 10:15 AM:
   9:50 AM  AMC         $2.97  range 2.90 to 2.96  +0.50% over
  ...
  10:15 AM  TSLA      $364.18  range 354.47 to 362.18  +0.55% over
```

The reel's wall had no price floor (AMC at $2.97 is one of the 42), hence
`--min-price 0`; with the default $5 floor it is 41. TSLA's range is the 9:30
to 9:40 bars, high 362.18, low 354.47; the 10:10 bar closed at 364.18, 0.55 %
over the high, at 10:15 AM. The user picks TSLA and Claude hands it to
`/trader-news TSLA`, then `/trader-entry TSLA`. In the reel the universe was
5,259 US stocks; this tool's rule gives 5,943 on the same files.

The pre-market scan that morning (closes through Fri Oct 16, the reel's daily
charts) tags BLK, CI, XOM and HD a 20-day breakout, CCL a bull flag, F, JPM
and GE a reclaim of the 20-day average, and BAC, MS, RIVN and PLUG oversold,
as the reel did. On closes it also tags KO (breakout), RCL (flag) and PNC (an
oversold bounce).

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable, and the time of the data.
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls data real time, and never promises that a setup works.

## Paper short structure

`orb --side sell --no-write --json` mirrors both stages: closes at/below
the lowest range close are candidates, then the first completed close
strictly below the real opening-range low confirms `orb_short`. Output
preserves `side: sell`, `analysis_only: true`, and `paper: true`. Daily
setups remain long; a bullish daily tag does not confirm a short.

For stock CFDs, pass `orb --instrument FILE --offline CHART --no-write --json`
and scan only verified OHLC bars of the exact contract identifier. Contract
metadata and exact quote precision are preserved in the scanner output.
Never substitute the underlying stock's prices. Contract terms,
instrument mapping and price basis are checked by sentiment, entry, exit
and risk. No live CFD data adapter or broker connection is provided.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
