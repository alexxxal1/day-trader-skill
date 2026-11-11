"""Versioned OHLC store and point-in-time views for scanner adapters."""
import copy
import datetime as dt
import math

from common import Clock, EPOCH, digest, iso, stamp


def number(value, positive=True):
    return type(value) in (float, int) and math.isfinite(value) and (value > 0 if positive else value >= 0)


class MarketData:
    """Rows contain start/end/available_at and OHLCV, optionally multiple vintages.

    Later vintages never replace an earlier one in an as-of view. Retrospective
    provider snapshots must explicitly say so in provenance; their assumed
    close-time availability is flagged, never presented as vintage truth.
    """
    def __init__(self, document):
        self.document = copy.deepcopy(document)
        self.clock = Clock(document["sessions"])
        self.days = sorted(document["sessions"])
        self.universe = sorted(set(document["universe"]))
        if not self.universe or set(self.universe) & {"SPY", "QQQ"}:
            raise ValueError("nonempty stock universe required; benchmarks are separate")
        self.rows = {}
        self.by_day = {}
        self._bars_cache = {}
        self.issues = []
        self.flags = []
        provenance = document.get("provenance", {})
        if not provenance.get("point_in_time_universe_includes_delisted", False):
            self.flags.append("SURVIVORSHIP_BIAS_RISK")
        if provenance.get("retrospective_snapshot", False):
            self.flags.append("REVISED_DATA_NOT_POINT_IN_TIME")
        self.flags.extend(["PRICE_RETURNS_EXCLUDE_DIVIDENDS", "EXPLORATORY_RESEARCH"])
        for sym, frames in document.get("bars", {}).items():
            for frame, rows in frames.items():
                if frame not in ("1d", "1m", "5m"):
                    raise ValueError("unsupported interval")
                groups = {}
                for raw in rows:
                    row = dict(raw, symbol=sym, interval=frame)
                    start, end, available = [stamp(row[k]) for k in ("start", "end", "available_at")]
                    if not start < end <= available:
                        raise ValueError("OHLC cannot be available before its bar closes")
                    if not all(number(row.get(k)) for k in ("o", "h", "l", "c")) or not number(row.get("v"), False):
                        self.issues.append({"symbol": sym, "frame": frame, "start": row["start"], "issue": "INVALID_OHLCV"})
                        continue
                    if not row["l"] <= min(row["o"], row["c"]) <= max(row["o"], row["c"]) <= row["h"]:
                        raise ValueError("inconsistent OHLC")
                    day = self.clock.to_eastern(start).date()
                    hours = self.clock.session(day)
                    if not hours:
                        self.issues.append({"symbol": sym, "frame": frame, "start": row["start"], "issue": "OUTSIDE_CALENDAR"})
                        continue
                    if frame == "1d" and (end != self.clock.close(day) or self.clock.to_eastern(start).time() != hours[0]):
                        raise ValueError("daily timestamps must describe the full exchange session")
                    if frame != "1d" and end - start != dt.timedelta(minutes=int(frame[:-1])):
                        raise ValueError("incorrect intraday duration")
                    row.update(_start=start, _end=end, _available=available, day=day.isoformat())
                    key = iso(start)
                    group = groups.setdefault(key, [])
                    if any(r["_available"] == available and any(r[k] != row[k] for k in ("o", "h", "l", "c", "v")) for r in group):
                        raise ValueError("conflicting vintages at the same availability time")
                    group.append(row)
                self.rows[sym, frame] = [sorted(group, key=lambda r: r["_available"]) for _, group in sorted(groups.items())]
                if any(len(group) > 1 for group in self.rows[sym, frame]) and "OUTCOMES_USE_LATEST_REPORTED_VINTAGE" not in self.flags:
                    self.flags.append("OUTCOMES_USE_LATEST_REPORTED_VINTAGE")
                for versions in self.rows[sym, frame]:
                    self.by_day.setdefault((sym, frame, versions[0]["day"]), []).append(versions)
        self.evidence = document.get("evidence", {"events": [], "metadata": []})
        for observations in document.get("borrow", {}).values():
            for row in observations:
                stamp(row["available_at"])
                if row.get("day") not in self.days or not number(row.get("cost_per_share"), False):
                    raise ValueError("borrow fees require a session, precise availability and nonnegative per-share cost")

    def bars(self, sym, frame, now=None, day=None):
        key = (sym, frame, day)
        if now is None and key in self._bars_cache:
            return self._bars_cache[key]
        result = []
        source = self.by_day.get((sym, frame, day), []) if day else self.rows.get((sym, frame), [])
        for versions in source:
            eligible = [r for r in versions if now is None or r["_available"] <= now]
            if eligible:
                row = eligible[-1]
                if day is None or row["day"] == day:
                    result.append(row)
        if now is None:
            self._bars_cache[key] = result
        return result

    def execution_bars(self, sym, frame, day=None):
        """Execution never substitutes a correction published after bar end."""
        key = (sym, frame, day, "execution")
        if key not in self._bars_cache:
            source = self.by_day.get((sym, frame, day), []) if day else self.rows.get((sym, frame), [])
            result = []
            for versions in source:
                known = [r for r in versions if r["_available"] <= r["_end"]]
                if known:
                    result.append(known[-1])
            self._bars_cache[key] = result
        return self._bars_cache[key]

    def series(self, sym, frame, now):
        day = self.clock.to_eastern(now).date().isoformat() if frame != "1d" else None
        rows = self.bars(sym, frame, now, day=day)
        # All snapshots use one documented provider split-adjusted basis. The
        # existing normalizer undoes splits later than the replay clock.
        out = {k: [r[k] for r in rows] for k in ("o", "h", "l", "c", "v")}
        out.update(t=[(r["_start"] - EPOCH).total_seconds() for r in rows],
                   meta={"dataGranularity": frame}, price_basis="YAHOO_SPLIT_ADJUSTED",
                   splits=self.document.get("splits", {}).get(sym, {}))
        return out

    def daily(self, now, symbols=None):
        return {s: self.series(s, "1d", now) for s in (symbols or self.universe)}

    def basis_factor(self, sym, now):
        """Translate snapshot split-adjusted quotes onto the decision's basis."""
        day, factor = self.clock.to_eastern(now).date(), 1.0
        for event in self.document.get("splits", {}).get(sym, {}).values():
            if self.clock.ts_to_eastern(event["date"]).date() > day:
                factor *= float(event["numerator"]) / float(event["denominator"])
        return factor

    def events(self, now):
        out = {"events": [], "metadata": []}
        for kind in out:
            for raw in self.evidence.get(kind, []):
                row = dict(raw)
                # Disclosure and correction dates override transaction dates.
                keys = ["published_at", "available_at"] + [k for k in ("disclosure_timestamp", "filing_at", "revised_at") if row.get(k)]
                try:
                    available = max(stamp(row[k]) for k in keys)
                except (ValueError, KeyError, TypeError):
                    continue
                if row.get("source_skill") == "trader-quiver" and not any(row.get(k) for k in ("disclosure_timestamp", "filing_at")):
                    continue
                if available <= now:
                    row["available_at"] = iso(available)
                    out[kind].append(row)
        return out

    def timeline(self, start, end):
        return [d for d in self.days if start <= d <= end]

    def complete_history(self, sym, now, n=260):
        rows = self.bars(sym, "1d", now)
        completed = [d for d in self.days if self.clock.close(d) <= now]
        return len(rows) >= n and [r["day"] for r in rows[-n:]] == completed[-n:]

    def audit(self, start, end):
        sessions = self.timeline(start, end)
        symbols = self.universe + ["SPY", "QQQ"]
        coverage = {}
        for sym in symbols:
            rows = self.bars(sym, "1d")
            days = {r["day"] for r in rows}
            intra = self.bars(sym, "5m") or self.bars(sym, "1m")
            coverage[sym] = {"daily_bars": len(rows), "first": rows[0]["day"] if rows else None,
                             "last": rows[-1]["day"] if rows else None,
                             "missing_sessions": [d for d in sessions if d not in days],
                             "intraday_bars": len(intra), "intraday_sessions": len({r["day"] for r in intra}),
                             "intraday_first": intra[0]["day"] if intra else None,
                             "intraday_last": intra[-1]["day"] if intra else None}
        return {"input_sha256": digest(self.document), "universe": self.universe,
                "stock_count": len(self.universe), "period": [start, end], "sessions": len(sessions),
                "flags": self.flags, "coverage": coverage, "invalid_rows": self.issues,
                "provenance": self.document.get("provenance", {})}
