"""Explicit snapshot/ledger evidence for mapping prices onto frozen daily bars.

Evidence is supplied from archived snapshots, never inferred from a fetch date,
provider name, equal prices or an empty actions response. No network access.
"""
import datetime as dt
from fractions import Fraction
import math

from common import digest
from .model import CLOCK


class BasisError(ValueError):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _ledger(evidence, bars, splits, day):
    required = ("adjustment", "anchor_date", "complete_from", "complete_through",
                "bars_sha256", "splits_sha256", "evidence_id")
    if not isinstance(evidence, dict) or any(not evidence.get(k) for k in required):
        raise BasisError("BASIS_COMPATIBILITY_UNKNOWN", "Missing snapshot/ledger evidence")
    if evidence["adjustment"] != "split":
        raise BasisError("BASIS_INCOMPATIBLE", "Frozen storage requires a split-only daily basis")
    anchor, since, through = [dt.date.fromisoformat(evidence[k]).isoformat()
                              for k in ("anchor_date", "complete_from", "complete_through")]
    if not since <= day <= anchor <= through:
        raise BasisError("BASIS_COMPATIBILITY_UNKNOWN", "Ledger does not cover the session through its basis anchor")
    if evidence["bars_sha256"] != digest(bars) or evidence["splits_sha256"] != digest(splits):
        raise BasisError("BASIS_EVIDENCE_MISMATCH", "Snapshot or split-ledger checksum differs")
    events = {}
    for event in splits.values():
        date = CLOCK.ts_to_eastern(event["date"]).date().isoformat()
        ratio = Fraction(str(event["numerator"])) / Fraction(str(event["denominator"]))
        if ratio <= 0 or date > anchor:
            raise BasisError("BASIS_INCOMPATIBLE", "Split outside the certified anchor or invalid ratio")
        events[date] = events.get(date, Fraction(1)) * ratio
    return events, anchor


def _factor(events, day):
    factor = Fraction(1)
    for date, ratio in events.items():
        if date > day:
            factor *= ratio
    return factor


def resolve_basis(document, symbol, day, fetched, config):
    """Return verified conversion metadata or an explicit exclusion status.

    Quote = historical raw / source split factor. Storage is therefore
    quote * source factor / frozen daily factor; volume uses the inverse.
    """
    try:
        daily = document.get("provenance", {}).get("daily_price_basis", {}).get(symbol)
        daily_bars = document["bars"].get(symbol, {}).get("1d", [])
        daily_splits = document.get("splits", {}).get(symbol, {})
        daily_events, daily_anchor = _ledger(daily, daily_bars, daily_splits, day)
        adjustment = fetched["request"]["adjustment"]
        if any(b.get("adjustment") != adjustment for b in fetched["bars"]):
            raise BasisError("BASIS_EVIDENCE_MISMATCH", "Source adjustment differs from its request")
        source = None
        source_factor = Fraction(1)
        source_anchor = None
        if adjustment == "split":
            # External manifests bind an exact provider/feed/request and exact
            # normalized payload, including cached responses fetched earlier.
            source = fetched.get("basis_evidence") or config.get("source_basis_evidence", {}).get(digest(fetched["request"]))
            source_splits = source.get("splits", {}) if isinstance(source, dict) else {}
            source_events, source_anchor = _ledger(source, fetched["bars"], source_splits, day)
            overlap = min(daily_anchor, source_anchor)
            for date in set(daily_events) | set(source_events):
                if day < date <= overlap and daily_events.get(date, Fraction(1)) != source_events.get(date, Fraction(1)):
                    raise BasisError("BASIS_INCOMPATIBLE", "Conflicting split actions within certified overlap")
            source_factor = _factor(source_events, day)
        elif adjustment != "raw":
            raise BasisError("BASIS_INCOMPATIBLE", "Only raw or split-only source prices are supported")
        price_factor = float(source_factor / _factor(daily_events, day))
        if not math.isfinite(price_factor) or price_factor <= 0:
            raise BasisError("BASIS_INCOMPATIBLE", "Invalid conversion factor")
        return dict(status="VERIFIED", price_factor=price_factor,
            storage_basis="FROZEN_DAILY_SPLIT_BASIS", storage_basis_id=digest(daily),
            source_adjustment=adjustment, source_anchor=source_anchor, daily_anchor=daily_anchor,
            source_basis_id=digest(source) if source else "RAW_HISTORICAL_TRADE_BASIS",
            daily_evidence_id=daily["evidence_id"])
    except BasisError as exc:
        return dict(status=exc.status, detail=exc.detail)
    except (ValueError, TypeError, KeyError, AttributeError, ZeroDivisionError, OverflowError):
        return dict(status="BASIS_COMPATIBILITY_UNKNOWN", detail="Malformed snapshot or ledger evidence")
