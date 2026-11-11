# The Day Trader skill

16 Claude skills for day trading: scan thousands of charts, track breaking news, time every entry and exit, and replay every outcome before you risk a dollar.

Free, MIT. Core tools need no signup or API key. Optional live social data
from X requires an official API token and API access. The tools are plain
Python that read public data: Yahoo Finance charts, Nasdaq Trader's symbol
lists, SEC EDGAR and the House Clerk, free public Quiver disclosures, plus
optional official X and Quiver API data. Quiver API access requires an
authorized key and may require a paid plan; its public-web route needs neither.

One scans thousands of charts for setups. Another tracks breaking news on
every stock you watch. One watches a setup bar by bar and tells you the moment
to get in, and another sets your stop and your target before you do. And the
craziest one is the Time Machine: before you risk a dollar, it replays the
rest of the session 10,000 times from the stock's own recent bars, plus every
past day the same setup fired, and tells you the honest odds.

And no trade goes through until you say yes. Nothing in this pack can place
an order on its own. Paper trading is the default.

> **Looking for the original Trader skill?** Copy an investor's 13F, the
> stock screener, tax lots and rebalancing live in
> [github.com/Jakeschincariol/trader-skill](https://github.com/Jakeschincariol/trader-skill).
> This repo is its day-trading sequel: the Trader skill v2.

## Install

Paste this into Claude:

```
https://github.com/Jakeschincariol/day-trader-skill

install skill
```

Or as a plugin, in Claude Code:

```
/plugin marketplace add Jakeschincariol/day-trader-skill
/plugin install day-trader-skill@day-trader-skill
```

Claude Code namespaces plugin skills, so installed as a plugin they show up as
`/day-trader-skill:trader-scanner` and so on. Copy the folders instead if you want
plain `/trader-scanner`:

```bash
git clone https://github.com/Jakeschincariol/day-trader-skill.git
cp -r day-trader-skill/trader-* ~/.claude/skills/
```

Project-local instead of global: copy the same folders into your repo's
`.claude/skills/`. No Claude Code at all? Paste any single `SKILL.md` at the
top of a chat and it runs as a mode. You lose the Python tools, but the method
works.

The core tools need Python 3.8 or newer. Nothing to pip install for those tools.
The optional expanded daily-history accelerator uses NumPy and Pandas; its
tested versions and offline commands are in `trader-backtest/DAILY.md`. The SEC tools
(news filings, the analyst) ask you to identify yourself: put your name and
email in `trader/config.json` (`"sec_user_agent": "Your Name you@example.com"`)
or in the `SEC_USER_AGENT` environment variable.

## Historical backtests

`/trader-backtest` adds reproducible historical measurement of Minervini,
Qullamaggie Breakout, Episodic Pivot, Parabolic Short/Long and Breakout
Confluence. Existing strategy functions remain unchanged. It separates filter
forward returns from executable trades, models costs and portfolio limits,
and reports seeded random controls, SPY/QQQ, chronological IS/OOS and data gaps.

[Method and offline commands](trader-backtest/SKILL.md) |
[Daily-only reproduction](trader-backtest/DAILY.md) |
[Event methodology and reproduction](trader-backtest/EVENTS.md) |
[Repository contents](trader-backtest/REPOSITORY-AUDIT.md)

Git contains the infrastructure, configuration, documentation and tests.
Historical datasets, downloaded evidence, reports, matrices and generated audits
are local run artifacts under the ignored `trader-backtest/runs/` directory.
They are not supplied by a clone. Identical offline reproduction requires the
original local snapshot; fresh acquisition creates a different vendor snapshot.

The event layer separates daily observations from confirmed state transitions,
tests full-reset and 20-session-cooldown definitions, audits source quality
uniformly, and reports ALL/VALID, overlapping/nonoverlapping windows and event
IS/OOS. Matched 1,000-replicate controls and full return distributions remain
descriptive; clean-check labels do not establish complete corporate-action
history, independent samples or a profitable strategy.

The expanded daily study requests the full current scanner universe of 5,933
US-listed stocks and ADRs. Of these, 5,475 have a complete common daily-history
window on at least one date. It compares unchanged native rules over 1993-2026,
with no intraday prerequisite and 1,000 seeded daily signal controls. Current
listing survivorship, revisable data and unresolved extreme price movements
keep strategy-performance conclusions exploratory. The original pilot's local
artifacts are retained separately.

The first snapshot has eight predeclared stocks, daily history from July 2024,
and 60 sessions of intraday data. It is exploratory: survivorship bias and
revised vendor data are explicitly flagged. Missing catalysts, short-borrow
data and CFD financing are never inferred. This is a measurement tool, not
proof of profitability. No broker calls, orders, commits or pushes occur.

## The sixteen

| command | job | what it does |
| --- | --- | --- |
| `/trader-scanner` | Chart Scanner | Scans thousands of charts for setups; adds Minervini's Trend Template filter with explicitly labelled RS_PROXY. |
| `/trader-news` | News Monitor | Tracks breaking news on every stock you watch and flags what moves it. |
| `/trader-social` | Social Catalyst Monitor | Reviews Musk/Trump/company posts with source verification and per-ticker market/setup checks; X API only, Truth Social input only, paper analysis. |
| `/trader-entry` | Entry Timer | Watches a setup bar by bar and tells you the moment to get in. |
| `/trader-exit` | Exit Planner | Sets your stop and your target before you enter, so you know when to get out. |
| `/trader-backtest` | Historical Backtests | Replays unchanged strategies with availability clocks, costs, sizing, random controls and explicit sample/data limitations. |
| `/trader-timemachine` | Time Machine | Replays every possible outcome of a trade before you risk a dollar: 10,000 simulated sessions plus every past time the setup fired. |
| `/trader-risk` | Risk Manager | Checks every trade against your limits and stops anything over them. |
| `/trader-budget` | Budget Manager | Sizes every position to your budget and your max loss per trade. |
| `/trader-broker` | Order Executor | Sends approved orders to your broker, paper trading by default, never without your yes. |
| `/trader-alerts` | Price Alerts | Rings when a price, a stop or the opening bell hits. |
| `/trader-analyst` | Earnings Analyst | Reads earnings reports and sums up what changed. |
| `/trader-pelosi` | Pelosi Tracker | Reads Congress's stock-trade disclosures the minute they are filed. |
| `/trader-quiver` | Quiver Disclosure Reader | Reads Congress/Insider disclosures via free public pages, optional official API or user input; separates trade/filing dates and flags inconsistent delays. Research only. |
| `/trader-sentiment` | Retail Sentiment Analyst | Reads Plus500 +Insights screenshots, compares positioning with price action and checks the existing skills. Paper analysis only; incomplete evidence means KEIN TRADE. |
| `/trader-confluence` | Confluence Layer | Compares Minervini and independent Qullamaggie setups with separate context/risk components, source deduplication and availability checks; analysis only. |

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, flat by the close                                       (while it is open)
news / analyst / pelosi                                                        (on a schedule)
```

An example, the trade in the reel: Tesla, Monday October 19 2026, real data.

1. **`/trader-scanner`** at 10:15 AM ET: of the 231 charts in the reel's
   scan, 42 had closed above their 15-minute opening range. TSLA is one: range 354.47 to 362.18,
   the 10:10 candle closed at 364.18, 0.55 % over the top.
2. **`/trader-news`**: the morning's TSLA headlines, in New York time
   ("Tesla-SpaceX Merger Mania Strikes Wall Street Ahead of Earnings", 8:04,
   tagged earnings and deal). Tesla reports this week: a day trade is out
   before the close, so it is not exposed to the report.
3. **`/trader-entry`** watched TSLA bar by bar. At 10:12 it said waiting (the
   10:10 bar was still forming). At 10:15 it said triggered: the first
   5-minute close above the range, 364.18.
4. **`/trader-exit`** set the plan before the entry: stop at the range's
   midpoint, 358.32, so 5.86 a share at risk; target one times that risk,
   370.04; flat by 3:55 PM either way.
5. **`/trader-timemachine`** replayed the rest of the day 10,000 times, each
   5-minute candle drawn from TSLA's own candles at that time of day over the
   previous 59 sessions (seed 19, so anyone can repeat it): target first
   29.4 %, stop first 25.5 %, still open at the close 45.1 %. It also re-ran
   the same rule on those 59 real sessions: it fired 33 times, 13 hit the
   target, 15 the stop, 5 neither. Its verdict: close to a coin flip. That is
   the point of looking before you risk a dollar.
6. **`/trader-budget`** with a $5,000 account and a $50 max loss per trade:
   50 / 5.86 = 8 shares ($2,913.44). If the stop fills at 358.32, the loss is
   $46.88. It writes one bracket order: buy 8 TSLA, stop-loss 358.32,
   take-profit 370.04.
7. **`/trader-risk`** checks it against your limits: the max loss, the daily
   loss limit, trades per day, reward to risk, the entry window, position
   size and cash. PASS or BLOCK, with the reason.
8. **`/trader-broker`** shows you the ticket in plain words. You type yes for
   that exact order. Claude calls your brokerage connector (Alpaca's MCP
   server, paper account by default), and Claude's own "Allow once / Deny"
   card asks you one more time.
9. **`/trader-alerts`** rings on the stop, the target and at 3:55 PM to
   flatten whatever is still open.

## The tools

All standard-library Python. The data tools read public endpoints and are
polite about it.

```bash
python3 trader-scanner/scan.py universe                                # ~5,900 US stocks, Nasdaq Trader
python3 trader-scanner/scan.py daily                                   # pre-market setups, whole universe
python3 trader-scanner/scan.py orb --at 10:15                          # opening-range breakouts so far
python3 trader-scanner/scan.py minervini                              # Trend Template filter -> trader/minervini.json
python3 trader-scanner/scan.py minervini --sort liquidity --show 10    # strongest PASS candidates, no entry signal
python3 trader-news/news.py --tickers TSLA --hours 12                  # headlines, 8-Ks, today's move
python3 trader-social/social.py analyze --posts trader/social-posts.json --evidence trader/social-evidence.json --json
python3 trader-social/social.py x --account elonmusk                  # optional X_BEARER_TOKEN + api.x.com
python3 trader-entry/entry.py check TSLA --setup orb                   # waiting / triggered / expired
python3 trader-entry/entry.py watch TSLA --every 30                    # bar by bar until it triggers
python3 trader-exit/exit.py plan --stop or-mid --target 1R --write     # -> trader/plan.json
python3 trader-timemachine/timemachine.py run --write                  # 10,000 replays + the history
python3 trader-budget/size.py --write                                  # -> trader/orders.json
python3 trader-risk/check.py                                           # PASS / BLOCK per order
python3 trader-broker/ticket.py show                                   # the tickets, READY or not
python3 trader-alerts/alerts.py add --from-plan                        # stop, target, flat by close
python3 trader-alerts/alerts.py check                                  # rings what hit, once
python3 trader-analyst/facts.py TSLA                                   # quarters, growth, EPS
python3 trader-pelosi/ptr.py index --member pelosi                     # her PTRs this year
python3 trader-quiver/quiver.py fetch --dataset congress --source public-web --json
python3 trader-quiver/quiver.py fetch --dataset insider --source public-web --json
python3 trader-quiver/quiver.py input --dataset congress --file trader-quiver/input.example.csv
python3 trader-sentiment/sentiment.py trader-sentiment/screenshot.example.json --extract-only
python3 trader-confluence/confluence.py --minervini trader/minervini.json --qullamaggie trader/qullamaggie-breakout.json --json
```

`trader-sentiment` uses the assistant's image reading to transcribe a screenshot;
its Python helper validates that JSON offline and never connects a broker or
places an order. See [its skill](trader-sentiment/SKILL.md) for the evidence
contract and a synthetic example. CFD and short candidates remain analysis
biases with `KEIN TRADE`: the current entry/exit pipeline plans equity longs.

```bash
python3 -m unittest discover -s tests -v
```

`/trader-scanner minervini` adds all ten Trend Template criteria, with at least
260 completed daily OHLC bars and a 21-session SMA200 trend comparison. PASS
is a momentum / Stage-2 research candidate, never an automatic LONG signal.
The transparent `RS_PROXY` ranks 252-interval split-adjusted price performance
across the full eligible US universe on a 0-100 percentile scale; it is **not**
the proprietary IBD / Minervini RS rating. Report selections do not shrink the
rank population. Missing/stale history fails, reduced coverage is PARTIAL,
and `trader/minervini.json` preserves the daily/orb watchlist. See the
[scanner skill](trader-scanner/SKILL.md) for the formula, all fields, split and
time handling, live/offline commands, liquidity sorting and proxy limitations.
Follow up with news, social, Quiver, sentiment, entry and risk evidence.

The tests run offline. Minervini tests use synthetic OHLC, split events and
mocked Yahoo responses, including threshold and existing-mode regressions.
Social tests use explicitly synthetic scenarios and
mocked API responses, never claims about actual posts. Other fixtures are cut
from real data: TSLA's 5-minute bars for the 60 days to October 19 2026, the
10:15 AM opening-range scan, Yahoo's
TSLA headlines that morning, the House Clerk's 2026 index, real PTR text and
real XBRL facts. They reproduce every number in the example above.

`trader-social` never sends orders. A post alone cannot produce a trading
candidate: it needs fresh market evidence and six reviewed skill reports.
See [the social skill](trader-social/SKILL.md) for the local evidence contract,
watchlist, source policy and API requirements. The current native pipeline
supports stock long/short and verified USD stock-CFD long/short in paper
analysis. Social evidence must preserve the exact instrument, direction and
contract identity; missing market or sentiment confirmation means KEIN TRADE.

`trader-quiver` defaults to the free public Quiver webpages (`PUBLIC_WEB`).
It also supports the explicitly selected official API (`QUIVER_API`, optional
`QUIVER_API_KEY`) and local screenshots transcribed by the assistant, CSV,
JSON or copied tables (`USER_INPUT`). It never requires MCP, follows a login,
bypasses a paywall, crawls history or silently switches sources. Each public
fetch reads one overview page. Trade date, disclosure date, publication age
and disclosure delay remain separate; negative delays are `INCONSISTENT`
with a null normal delay. A new filing need not describe a recent trade.
No broker or orders are involved. See [the Quiver skill](trader-quiver/SKILL.md)
for the source policy, date precision, examples, API command and limitations.
Quiver tests use reduced public-page snapshots and synthetic API fixtures;
the full suite runs offline without a key.

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It does not know your situation and it cannot see the future.
Most people who day trade lose money. You can lose money. Decide for
yourself, and talk to a licensed professional if you need advice.

**No trade goes through until you say yes.** Only `/trader-broker` can lead to
an order, and only after `/trader-risk` passes that exact order and you type
yes for it in the chat. Set your brokerage tool to "Needs approval" in Claude
(Customize, Connectors, tool permissions), never "Always allow", so Claude's
own "Allow once / Deny" card is a second check. No options, margin, short
selling or leverage unless you turn them on.

**Paper first.** `"paper": true` is the default in `config.example.json`, and
Alpaca's MCP server defaults to its paper account. Keys live in environment
variables, never in the chat.

**The Time Machine is a simulation, not a prediction.** It is built from the
stock's own last 60 days of 5-minute bars. The future can do things those 60
days never did: an earnings report, a halt, a headline. Treat its odds as a
reality check, never as a promise.

**A stop is a plan, not a guarantee.** In a fast market or across a gap, a
stop order can fill well past its price. The max loss shown is the loss at
the stop price.

**The data is free and unofficial.** Yahoo Finance's chart endpoint is not an
official API: it can lag, change or rate-limit you. The scanner and the entry
timer read 1-minute or 5-minute bars, not every tick. Check anything that
matters against your broker before you act on it.

**Day-trading rules.** FINRA removed the $25,000 pattern-day-trader minimum
on June 4, 2026 (Regulatory Notice 26-10), and brokers may phase the change
in until October 20, 2027, so yours may still apply the old rule. If it does,
set `"pdt_guard": true` and `/trader-risk` counts your day trades.

**Congress's trades are public, not inside information.** Members must disclose
trades over $1,000 within 45 days (the STOCK Act). The Pelosi skill reads those
public filings the minute they post.

**Taxes.** Day-trading gains are short-term gains, and frequent trading makes
wash sales likely. Nothing here is tax advice.

## Files

```
trader-scanner/     SKILL.md, scan.py, minervini.py
trader-news/        SKILL.md, news.py
trader-social/      SKILL.md, social.py, x_api.py, watchlist.json
trader-entry/       SKILL.md, entry.py
trader-exit/        SKILL.md, exit.py
trader-timemachine/ SKILL.md, timemachine.py
trader-risk/        SKILL.md, check.py
trader-budget/      SKILL.md, size.py, config.example.json, targets.example.json
trader-broker/      SKILL.md, ticket.py, claude_desktop_config.example.json
trader-alerts/      SKILL.md, alerts.py, alerts.example.json
trader-analyst/     SKILL.md, facts.py, metrics.example.json
trader-pelosi/      SKILL.md, ptr.py
trader-quiver/      SKILL.md, quiver.py, sources.py, parsers.py, input.example.json, input.example.csv
tests/              unittest suite + offline fixtures
```

MIT. Made by [Jake Schincariol](https://opusjake.ai).

## Qullamaggie research filters

`/trader-scanner qullamaggie-breakout`, `qullamaggie-ep` and
`qullamaggie-parabolic` add independent research reports for momentum bases,
episodic pivots and parabolic short/long variants. They expose ranks, setup
metrics, causal catalyst timestamps and completed-bar triggers without
combining Minervini, placing orders or granting risk approval. See
[the exact rules, examples and limits](trader-scanner/QULLAMAGGIE.md).

`/trader-confluence` reads these existing reports without changing their rules.
It distinguishes filter/setup/trigger agreement, EP-only and Qullamaggie-only
setups, conflicts and insufficient data. All variants remain visible per ticker;
news, social, Quiver, sentiment and full risk verdicts remain separate components.
Shared primary events count once, delayed disclosures stay context, and reports
cannot supply data before its actual availability. Risk FAIL means NO_TRADE;
confluence never grants order permission. See [the input contract and rule table](trader-confluence/SKILL.md)
and the [synthetic example](trader-confluence/input.example.json). The helper is
offline, standard-library Python and writes only an optional analysis report.
