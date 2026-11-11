# Repository contents and artifact policy

The versioned backtest infrastructure covers the original trade engine, the
expanded daily study and the event layer. Only source code, tests, configuration,
documentation and ignore rules belong in Git. Run output is not a test fixture.

## Infrastructure and tests included in Git

| Capability | Implementation |
| --- | --- |
| Backtest orchestration, temporal market data, collection | `run.py`, `common.py`, `data.py`, `collect.py` |
| Trade engine, costs, long/short P&L, sizing, portfolio limits | `engine.py` |
| Read-only native strategy adapters | `adapters.py`, `strategy-sources.json` |
| Daily historical acquisition and frozen snapshot diagnosis | `daily_collect.py`, `diagnose.py` |
| Native daily replay and historical eligible universes | `daily_history.py`, `daily_verify.py` |
| Forward returns and trade/portfolio metrics | `metrics.py`, `daily_results.py` |
| Seeded signal and portfolio random baselines | `baseline.py`, `daily_results.py` |
| Chronological IS/OOS, purging and walk-forward windows | `splits.py`, `run.py`; daily/event IS/OOS also in their statistics modules |
| Full-reset events and 20-session cooldown | `event_signals.py`, `event-methodology.json` |
| Uniform VALID/SUSPECT/INVALID data-quality audit | `data_quality.py`, `event_audit.py` |
| Event forward distributions, overlap and block bootstrap | `event_statistics.py` |
| Event random controls and resumable grouped engine | `event_random.py`, `event_random_grouped.py` |
| Reporting | `reporting.py`, `daily_report.py`, `event_report.py` |
| Documentation and configuration | `SKILL.md`, `DAILY.md`, `EVENTS.md`, `config.first.json`, `requirements-daily.txt` |
| All backtest tests | `../tests/test_backtest.py`, `../tests/test_daily_backtest.py`, `../tests/test_event_backtest.py`; existing repository tests retained |

`strategy-sources.json` pins the native strategy sources for invariance tests;
`event-methodology.json` defines the source protocol, and `config.first.json`
is a small runnable example configuration. These are configuration, not run
results. `requirements-daily.txt` pins the optional daily/event dependencies.

## Local run artifacts

Everything under `runs/` is ignored: provider responses, downloaded prices,
normalised datasets, exchange calendars acquired for a run, matrices, per-stock
caches, signal/forward ledgers, reports, result summaries, downloaded SEC
evidence, frozen run copies and random checkpoints/accumulators. This includes
the original eight-stock pilot; it is not required to execute the tests.

Generated file inventories, Git-status captures and test-validation output under
`repository-audit/` are also ignored. Python caches, test-tool caches, coverage
output and temporary/log files stay local. The `.gitignore` rules do not exclude
the source configuration files or documentation.

Removing artifacts from Git does not delete the original workspace copies.
A clone supplies the complete infrastructure but no historical snapshot.
Identical offline reproduction needs the original local inputs; hashes cannot
substitute for their contents. Fresh acquisition creates a new, revisable
snapshot and must not be represented as the identical experiment.

## Tests and fixtures

The three backtest test modules construct small deterministic synthetic inputs
in Python and temporary directories. They do not depend on the pilot, expanded
history, downloaded corporate-action evidence, reports or cached matrices.
Existing small repository fixtures under `tests/fixtures/` remain unchanged.
No additional downloaded dataset is retained as a backtest fixture.

Run the complete offline suite from the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
```

Daily-history and intraday-trigger results remain separate. Existing performance
limitations, including survivorship bias and incomplete security identity
history, remain part of the methodology. This packaging policy changes no
strategy rule, parameter or Python implementation.
