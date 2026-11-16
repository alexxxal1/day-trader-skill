"""Provider-neutral requests and UTC interval-start bars. Python 3.8 compatible."""
import datetime as dt
import math
from dataclasses import dataclass, asdict

from common import Clock, EPOCH, calendar_2024_2027, iso, stamp

TIMEFRAMES = {"1m": 1, "5m": 5, "15m": 15, "60m": 60}
CLOCK = Clock(calendar_2024_2027())


@dataclass(frozen=True)
class BarRequest:
    ticker: str
    timeframe: str
    start: str
    end: str
    adjustment: str = "raw"

    def __post_init__(self):
        aliases = {"1Min": "1m", "5Min": "5m", "15Min": "15m", "60Min": "60m", "1Hour": "60m"}
        object.__setattr__(self, "timeframe", aliases.get(self.timeframe, self.timeframe))
        if not self.ticker or not all(c.isalnum() or c in ".-" for c in self.ticker):
            raise ValueError("invalid US stock ticker")
        if self.timeframe not in TIMEFRAMES or self.adjustment not in ("raw", "split"):
            raise ValueError("unsupported timeframe/adjustment")
        begin, finish = stamp(self.start), stamp(self.end)
        if begin >= finish or begin.year < 2007:
            raise ValueError("positive interval, US timezone rules supported since 2007")
        if begin.microsecond or finish.microsecond:
            raise ValueError("request boundaries require whole-second precision")
        object.__setattr__(self, "ticker", self.ticker.upper())
        object.__setattr__(self, "start", iso(begin))
        object.__setattr__(self, "end", iso(finish))

    def key(self, provider, feed):
        return dict(asdict(self), provider=provider, feed=feed, schema_version=1)


def session_at(utc, sessions=None):
    """Use the supplied exchange calendar, including early closes.

    Outside its year range labels describe clock windows only, never verified
    exchange sessions. Historical audits expose this limitation explicitly.
    """
    sessions = CLOCK.sessions if sessions is None else sessions
    local = CLOCK.to_eastern(utc)
    day = local.date().isoformat()
    hours = sessions.get(day)
    verified = bool(sessions) and min(sessions)[:4] <= day[:4] <= max(sessions)[:4]
    if (verified and not hours) or local.weekday() >= 5:
        return "CLOSED", "VERIFIED" if verified else "CLOCK_ONLY_UNVERIFIED"
    op, close = [dt.time.fromisoformat(t) for t in hours] if hours else (dt.time(9, 30), dt.time(16))
    t = local.time()
    label = ("PREMARKET" if dt.time(4) <= t < op else "REGULAR" if op <= t < close else
             "AFTERHOURS" if close <= t < dt.time(20) else "CLOSED")
    return label, "VERIFIED" if verified else "CLOCK_ONLY_UNVERIFIED"


def normalize_bar(request, timestamp, values, provider, feed, sessions=None):
    utc = stamp(timestamp)
    if utc.second or utc.microsecond:
        raise ValueError("intraday aggregates require minute-aligned starts")
    local = CLOCK.to_eastern(utc)
    offset = dt.timezone(local - utc)
    session, calendar_status = session_at(utc, sessions)
    return dict(ticker=request.ticker, timestamp_utc=iso(utc),
        timestamp_new_york=local.replace(tzinfo=offset).isoformat(), timezone="America/New_York",
        open=values.get("o"), high=values.get("h"), low=values.get("l"), close=values.get("c"),
        volume=values.get("v"), vwap=values.get("vw"), trade_count=values.get("n"),
        provider=provider, feed=feed, adjustment=request.adjustment, session=session,
        session_calendar_status=calendar_status, timeframe=request.timeframe)


def validate_bars(bars, request):
    """Report, never interpolate. Gaps/jumps are diagnostics, not PIT filters.

    A gap can mean no qualifying trades (particularly on IEX), a halt, or a
    missing response. Its cause cannot be inferred from aggregates alone.
    """
    errors, warnings = [], []
    previous, seen = None, set()
    step = dt.timedelta(minutes=TIMEFRAMES[request.timeframe])
    for i, bar in enumerate(bars):
        def issue(code, detail=None, fatal=True):
            (errors if fatal else warnings).append(dict(index=i, code=code, detail=detail))
        try:
            at = stamp(bar["timestamp_utc"])
            local = dt.datetime.fromisoformat(bar["timestamp_new_york"])
            expected = CLOCK.to_eastern(at)
            if stamp(local) != at or local.replace(tzinfo=None) != expected:
                issue("TIMEZONE_ERROR")
            if not stamp(request.start) <= at < stamp(request.end):
                issue("OUTSIDE_REQUEST")
            if at in seen:
                issue("DUPLICATE_TIMESTAMP")
            if previous and at < previous[0]:
                issue("UNSORTED_TIMESTAMPS")
            seen.add(at)
            if bar.get("ticker") != request.ticker or bar.get("timeframe") != request.timeframe:
                issue("IDENTITY_MISMATCH")
            o, h, l, c, v = [bar.get(k) for k in ("open", "high", "low", "close", "volume")]
            numeric = all(type(x) in (int, float) and math.isfinite(x) and x > 0 for x in (o, h, l, c))
            if not numeric or type(v) not in (int, float) or not math.isfinite(v) or v < 0:
                issue("INVALID_OHLCV")
            elif not l <= min(o, c) <= max(o, c) <= h:
                issue("INCONSISTENT_OHLC")
            else:
                if previous and previous[1]["close"] and at.date() == previous[0].date():
                    ratio = c / previous[1]["close"]
                    if ratio >= 2 or ratio <= .5:
                        issue("EXTREME_JUMP", ratio, False)
                if previous and at.date() == previous[0].date() and at - previous[0] > step:
                    issue("MISSING_BAR_SLOTS", int((at - previous[0]) / step) - 1, False)
            for key in ("vwap", "trade_count"):
                value = bar.get(key)
                if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                    issue("INVALID_" + key.upper())
            previous = at, bar
        except (ValueError, KeyError, TypeError, OverflowError):
            issue("INVALID_TIMESTAMP_OR_SCHEMA")
    # Check regular-session edges only when the full interval was requested.
    observed = {b["timestamp_utc"] for b in bars}
    for day, hours in CLOCK.sessions.items():
        op = CLOCK.eastern_to_utc(dt.datetime.combine(dt.date.fromisoformat(day), dt.time.fromisoformat(hours[0])))
        close = CLOCK.close(day)
        if stamp(request.start) <= op and stamp(request.end) >= close:
            count = sum(iso(t) not in observed for t in (op, close - step))
            if count:
                warnings.append(dict(code="MISSING_SESSION_EDGES", session=day, missing_edges=count))
    return dict(status="INVALID" if errors else "SUSPECT" if warnings else "VALID",
                errors=errors, warnings=warnings, interpolated=False)


def execution_row(bar):
    """Existing replay contract: data become observable no earlier than close."""
    end = stamp(bar["timestamp_utc"]) + dt.timedelta(minutes=TIMEFRAMES[bar["timeframe"]])
    return dict(zip(("o", "h", "l", "c", "v"), [bar[k] for k in ("open", "high", "low", "close", "volume")]),
        start=bar["timestamp_utc"], end=iso(end), available_at=iso(end),
        provider=bar["provider"], feed=bar["feed"], adjustment=bar["adjustment"],
        vwap=bar["vwap"], trade_count=bar["trade_count"], session=bar["session"],
        availability_assumption="retrospective bar-close proxy; NOT_HISTORICALLY_PIT_CERTIFIED")


def validated_observations(bars, request):
    """Preserve valid observations in response order, never revise a prefix.

    A structural error stops admission at that observation. The duplicate or
    out-of-order revision and its suffix are quarantined, not the earlier
    observations. Numerical errors create holes; no replacement is invented.
    Response order is not evidence of historical delivery vintages.
    """
    quality = validate_bars(bars, request)
    numerical = {"INVALID_OHLCV", "INCONSISTENT_OHLC", "INVALID_VWAP", "INVALID_TRADE_COUNT"}
    structural = [e for e in quality["errors"] if e["code"] not in numerical]
    cutoff = min((e["index"] for e in structural), default=len(bars))
    rejected = {e["index"] for e in quality["errors"]} | set(range(cutoff, len(bars)))
    usable = [b for i, b in enumerate(bars) if i not in rejected]
    quarantined = [dict(index=i, timestamp_utc=b["timestamp_utc"],
        reason="AT_OR_AFTER_STRUCTURAL_ERROR" if i >= cutoff else "INVALID_OBSERVATION")
        for i, b in enumerate(bars) if i in rejected]
    boundary = dict(index=cutoff, timestamp_utc=bars[cutoff]["timestamp_utc"],
        codes=sorted({e["code"] for e in structural if e["index"] == cutoff})) if structural else None
    return usable, quality, quarantined, boundary
