#!/usr/bin/env python3
"""Add provider intraday data to a frozen daily context; resume by stock/session.

No daily acquisition, strategy evaluation or reference download is implicit.
The original Yahoo collector remains available unchanged.
"""
import argparse
import copy
import datetime as dt
from pathlib import Path

from common import digest, iso, read, write
from data import MarketData
from intraday_providers import BarRequest, configured, execution_row
from intraday_providers.cache import atomic_write
from intraday_providers.basis import resolve_basis
from true_trade_collect import freeze
from true_trade_execution import opening


def collect(document, provider_config, output, start, end, frames=("1m", "5m"), router=None):
    if start > end or not frames or any(f not in ("1m", "5m") for f in frames):
        raise ValueError("ordered dates and native 1m/5m frames required")
    if provider_config.get("mixed_provider_primary", False):
        raise ValueError("mixed provider data are excluded from primary True Trades")
    output = Path(output)
    freeze(output / "frozen-provider-acquisition.json", dict(context_sha256=digest(document),
        provider_config=provider_config, start=start, end=end, frames=list(frames), basis_policy_version=1))
    market = MarketData(document)
    router = router or configured(provider_config, sessions=document["sessions"])
    manifest_path = output / "progress.json"
    manifest = read(manifest_path) if manifest_path.exists() else dict(completed={}, errors=[])
    manifest.setdefault("pinned", {})
    result = copy.deepcopy(document)
    # Replace intraday as a whole; a prior provider's bars must never survive a
    # partially successful rerun. Daily bars and setup evidence remain frozen.
    for symbol in market.universe:
        result["bars"][symbol] = {"1d": copy.deepcopy(document["bars"].get(symbol, {}).get("1d", [])), "1m": [], "5m": []}
    provenance = result.setdefault("provenance", {})
    provenance.update(retrospective_snapshot=True, intraday_provider_layer=True,
        historical_certification="NOT_HISTORICALLY_PIT_CERTIFIED", provider_records=[],
        intraday_basis="Only explicitly verified snapshot/ledger conversions enter frozen daily storage; source adjustment stays separate")
    for day in market.timeline(start, end):
        op, close = opening(market.clock, day), market.clock.close(day)
        for symbol in market.universe:
            key = symbol + "/" + day
            chunk = output / "chunks" / (symbol + "-" + day + ".json")
            if key in manifest["completed"]:
                payload = read(chunk)
                if digest(payload) != manifest["completed"][key]:
                    raise ValueError("acquisition checkpoint checksum mismatch")
            else:
                selected = router.fetch_trade([BarRequest(symbol, f, iso(op), iso(close)) for f in frames],
                    opening=iso(op), pinned_provider=manifest["pinned"].get(key))
                payload = dict(ticker=symbol, session=day, selection=selected, status=selected["status"], bars={}, basis={})
                if selected["status"] == "AVAILABLE":
                    manifest["pinned"][key] = selected["provider"]
                    for fetched in selected["results"]:
                        frame = fetched["request"]["timeframe"]
                        if not fetched["bars"]:
                            continue
                        basis = resolve_basis(document, symbol, day, fetched, provider_config)
                        payload["basis"][frame] = basis
                        if basis["status"] != "VERIFIED":
                            continue
                        rows = []
                        for bar in fetched["bars"]:
                            row = execution_row(bar)
                            factor = basis["price_factor"]
                            row.update({k: row[k] * factor for k in ("o", "h", "l", "c")})
                            row["v"] /= factor
                            if row["vwap"] is not None:
                                row["vwap"] *= factor
                            row.update(storage_basis=basis["storage_basis"], storage_basis_id=basis["storage_basis_id"],
                                storage_adjustment="split", basis_status="VERIFIED", basis_price_factor=factor,
                                source_adjustment=basis["source_adjustment"], source_basis_id=basis["source_basis_id"])
                            rows.append(row)
                        payload["bars"][frame] = rows
                    chosen = selected["selection_basis"]["timeframe"]
                    if payload["basis"].get(chosen, {}).get("status") != "VERIFIED":
                        payload["status"] = payload["basis"].get(chosen, {}).get("status", "BASIS_COMPATIBILITY_UNKNOWN")
                        payload["bars"] = {}
                atomic_write(chunk, payload)
                incomplete = any(r.get("download_complete") is False for r in selected["results"]
                    if r["status"] not in ("INTRADAY_HISTORY_LIMITED", "UNSUPPORTED_ADJUSTMENT", "EMPTY"))
                if payload["status"] == "AVAILABLE" and not incomplete:
                    manifest["completed"][key] = digest(payload)
                else:
                    manifest["errors"].append(dict(key=key, status=payload["status"] if payload["status"] != "AVAILABLE" else "DOWNLOAD_INCOMPLETE",
                        attempts=selected["attempts"], basis=payload["basis"]))
                    # Failure is not complete: the next invocation resumes it.
                atomic_write(manifest_path, manifest)
            for frame, rows in payload["bars"].items():
                result["bars"][symbol][frame].extend(rows)
            provenance["provider_records"].append(dict(ticker=symbol, session=day,
                provider=payload["selection"]["provider"], feed=payload["selection"]["feed"],
                status=payload["status"], attempts=payload["selection"]["attempts"], basis=payload["basis"]))
            print(key, payload["status"], payload["selection"]["provider"], flush=True)
    write(output / "dataset.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, help="Frozen existing dataset; daily/evidence are never re-downloaded")
    parser.add_argument("--providers", default=str(Path(__file__).with_name("intraday-provider-config.json")))
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    collect(read(args.context), read(args.providers), args.output, args.start, args.end)
