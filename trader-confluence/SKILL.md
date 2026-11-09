---
name: trader-confluence
description: Transparently compare the existing Minervini Trend Template with independent Qullamaggie breakout, episodic-pivot and parabolic long/short reports. Show each component, source availability, shared primary events, conflicts and remaining risk/entry checks per ticker. Analysis only, never an order or a new blended strategy.
---

# Confluence Layer

Use this after the existing scanner and optional context skills to explain
Minervini alone, Qullamaggie alone, agreement, conflict or insufficient evidence.
The helper reads local reports only, uses Python 3.8+ and the standard library,
and makes no network requests. Do not change any upstream strategy or threshold.
It never calls the broker or creates an order, entry, exit or budget plan.

## Run

```bash
python3 trader-confluence/confluence.py trader-confluence/input.example.json
python3 trader-confluence/confluence.py --minervini trader/minervini.json --qullamaggie trader/qullamaggie-breakout.json --json
python3 trader-confluence/confluence.py --minervini trader/minervini.json --qullamaggie trader/qullamaggie-breakout.json --qullamaggie trader/qullamaggie-ep.json --qullamaggie trader/qullamaggie-parabolic.json --news trader/news-report.json --social trader/social-report.json --quiver trader/quiver-report.json --sentiment trader/sentiment-report.json --risk trader/state/risk.json --out trader/confluence.json
```

Each report option is repeatable. Nothing is written unless `--out` is supplied.
Use a separate analysis filename; input files and common pipeline artifacts
cannot be overwritten. `--now 2026-11-09T15:15:00Z` pins the decision clock;
without it the bundle's `decision_at`, then current UTC is used. `--tickers`
adds comma-separated exact tickers, including ones with missing reports.

The native scanner reports must include `results`, including failures and
insufficient rows, rather than only `hits`. This preserves coverage and opposing
signals. Keep `created_by`, `mode`, timestamps, `daily_as_of`/`close_date`,
`variant`, `setup_status`, catalysts, trigger, stop and risk context unchanged.
The original scanner's FAIL for missing RS_PROXY/history becomes
INSUFFICIENT_DATA here; a genuine failed Trend Template remains FAIL.

## Explicit rules, no aggregate score

| Minervini | Qullamaggie | Confluence | Type |
| --- | --- | --- | --- |
| PASS | Breakout CANDIDATE | WATCH | FILTER_CONFLUENCE |
| PASS | Breakout SETUP_READY | LONG_SETUP | SETUP_CONFLUENCE |
| PASS | Breakout TRIGGERED | STRONG_LONG_CONFLUENCE | TRIGGER_CONFLUENCE |
| FAIL/INSUFFICIENT_DATA | Active breakout | QULLAMAGGIE_ONLY | none |
| PASS | Confirmed EP SETUP_READY/TRIGGERED | LONG_SETUP/STRONG_LONG_CONFLUENCE | ADDITIONAL_TREND_CONFLUENCE |
| FAIL/INSUFFICIENT_DATA | Confirmed EP SETUP_READY/TRIGGERED | EP_ONLY | none |
| any | EP CANDIDATE | WATCH | none |
| PASS | Parabolic short TRIGGERED | CONFLICT | none |
| any other combination | Parabolic short SETUP_READY/TRIGGERED | SHORT_SETUP | none |
| any | Parabolic short CANDIDATE | WATCH | none |
| any | Active parabolic long | QULLAMAGGIE_ONLY | none |
| PASS | No active setup | WATCH | none |
| FAIL/INSUFFICIENT_DATA | No active setup | NO_SETUP | none |

An EP must retain its native verified catalyst with reference and publication /
availability timestamps. A legitimate confirmed EP is never vetoed by Minervini.
Parabolic long after a collapse remains independent of a failed trend filter.
For a short conflict, current price action and the short trigger are more timely
than the medium-term trend; existing entry and risk rules must resolve it.
The layer does not automatically prefer a side.

Multiple variants remain in `setups`; the table displays their statuses and
levels separately. Opposing SETUP_READY/TRIGGERED setups yield CONFLICT.
Otherwise the furthest explicit stage is displayed: triggered, ready, candidate.
Within that stage, the display order is strong long, long setup, EP only,
short setup, Qullamaggie only, watch. This is a display rule, never a weighted
score, and two candidates cannot become a ready setup or a trigger.

## Components and trading direction

- MINERVINI: PASS / FAIL / INSUFFICIENT_DATA.
- QULLAMAGGIE: BREAKOUT / EP / PARABOLIC_SHORT / PARABOLIC_LONG / NONE.
- QULLAMAGGIE_STATUS: CANDIDATE / SETUP_READY / TRIGGERED / FAILED / INSUFFICIENT_DATA.
- NEWS and SOCIAL: CONFIRMED / NEUTRAL / CONFLICTING / UNAVAILABLE.
- QUIVER: CONFIRMED / NEUTRAL / CONFLICTING / STALE / UNAVAILABLE.
- SENTIMENT: CONFIRMING / CONTRARIAN / NEUTRAL / UNAVAILABLE.
- RISK: PASS / FAIL. Missing, expired or unmatched full risk evidence is FAIL.

Confluence describes analytical agreement independently of risk. Risk FAIL
always means direction NO_TRADE, including for strong confluence. LONG/SHORT
describes an existing completed trigger only when full risk evidence also
matches. WATCH is reserved for an untriggered setup with cleared risk evidence.
The current conservative native adapter requires a completed trigger to bind
full risk, so pending setups normally show NO_TRADE with their WATCH/LONG_SETUP
classification. CONFLICT always means NO_TRADE. Every output always carries
`trade_allowed: false`, `signal: null`, `analysis_only: true`.

Full risk means a native `trader-risk` result for an entry with the exact ticker,
direction, instrument type, contract, entry reference, stop, valid target and
order fingerprint. It must be fresh, dated at/after the trigger, PASS without
open reasons or warnings, and preserve upstream blocks/liquidity/stop warnings.
Scanner `risk.verdict: NOT_ASSESSED` is never approval. An analysis risk report
without an exact timestamp needs a separately retained `checked_at` timestamp
from the original evaluation; never manufacture it later. CFD prices must keep
their exact contract symbol. Never substitute the underlying's prices or risk
report for a CFD. This layer cannot extend the upstream scanner's asset support.

## Sources and reviewed context

Native headlines are neutral leads. Native verified social rows need price
confirmation before confirming direction. Native sentiment must have an
analysed LONG/SHORT decision and price-checked bias; screenshot extraction alone
cannot confirm. A contrary sentiment bias is visible in the evidence even
though the allowed sentiment enum has no CONFLICTING value.

Native Quiver/Congress/13F transaction direction and dark-pool activity are
context only. Their publication delay sharply limits short-term relevance;
a recently filed report can describe an old trade. Do not turn purchases,
sales, dark-pool activity or number of reports into automatic direction.

A bundle can contain `reports: [...]` and `events: [...]`. Reviewed events add
explicit causal context without editing the native report:

```json
{
  "symbol": "DEMO",
  "source_skill": "trader-news",
  "published_at": "2026-11-09T15:00:00Z",
  "available_at": "2026-11-09T15:01:00Z",
  "reference": "https://example.test/secondary-story",
  "primary_source": "https://x.com/company/status/12345",
  "event_id": "demo-primary-event",
  "confirmed": true,
  "direction": "LONG"
}
```

Publication fields are `published_at` for news, `posted_at` for social,
`disclosure_timestamp` for Quiver and `captured_at` for sentiment. All require
`available_at` and a reference. `confirmed: true` means an actual reviewed causal
assessment, never inference from a headline or raw post. Direction is LONG or
SHORT; `conflicting: true` flags verified mixed evidence. For sentiment,
`assessment: CONTRARIAN` identifies a price-confirmed contrarian bias. Quiver
`dataset: congress/13f/darkpool/dark-pool` is always context only.

News/Social/Quiver/Sentiment remain separate context components and never vote
a setup into a higher stage or override price action. Current conflicting
context is shown under the component and explanation; it does not replace the
Minervini/Qullamaggie classification. Existing entry/risk checks still decide.

## Time and source audit

No record is usable before both publication and observed availability. Unknown
timezone or date-only disclosure timestamps cannot establish intraday evidence.
Congress availability starts at filing/disclosure, never transaction date.
The exact latest completed exchange session is required for daily snapshots;
today's close cannot support an earlier intraday decision. The shared existing
entry calendar handles weekends, holidays, DST and early closes (2026-2027).

Snapshot freshness: Minervini up to seven calendar days with the latest completed
session; Qullamaggie and risk 15 minutes; news 24 hours; social four hours;
sentiment 15 minutes; Quiver seven days. EP catalysts retain the native 96-hour
window and must have been available at the trigger. Both publication age and
availability are checked, so fetching an old item now cannot rejuvenate it.
These are explicit context policies, not new strategy thresholds or numeric
votes. The JSON `horizons` explains each role.

Intraday triggers must be on completed regular-session bars. ORH/ORL triggers
must follow the closed 1/5/60-minute opening range; five-minute reversals need a
closed five-minute bar. Current-session triggers cannot be carried into the
next session or claimed outside trading hours. Future/expired snapshots,
future stops, future invalidations and equal-time contradictory snapshots are
insufficient. Invalid data is never upgraded to an entry.

The audit keeps source reports, excluded evidence, publication/observation times
and exact exclusion reasons. `event_id`, `primary_source`, canonical reference
URLs and X/Twitter post IDs are grouped transitively across channels. Shared
primary events count once; mutually conflicting claims do not confirm a group.
Unknown provenance cannot prove independence. The group count is explanatory
only and never controls confluence, direction or orders.

## Output and safety

One row per exact ticker: Minervini, each Qullamaggie variant/status, News,
Social, Quiver, Sentiment, Risk, Confluence, Direction, Trigger, Stop, Target,
Begründung. Never invent a target: show it only from an existing matching plan /
risk verdict. Follow with WHY interesting, WHAT confirms, WHAT opposes and WHAT
must still happen before a trade. Missing evidence stays explicit.

Not financial advice. Analysis is not execution approval. Any later order needs
the existing full pipeline and the user's yes for the exact ticket. This skill
does not place orders, call a broker or alter existing strategy logic.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```
