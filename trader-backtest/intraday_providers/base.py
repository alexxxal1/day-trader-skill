"""Provider contract. Only complete requests enter the reproducible cache."""
from abc import ABC, abstractmethod
import datetime as dt

from common import iso, stamp
from .model import TIMEFRAMES, validated_observations
from .transport import ProviderError


class IntradayProvider(ABC):
    name = None
    feed = None
    auth_required = True
    history_status = "HISTORY_UNVERIFIED"
    split_adjustment = True

    def __init__(self, client, cache=None, sessions=None):
        self.client, self.cache, self.sessions = client, cache, sessions
        self.observations = []
        self.halted_status = None

    @property
    @abstractmethod
    def authenticated(self):
        """Credentials present; this does not claim remote authorization."""

    def supported_timeframes(self):
        return tuple(TIMEFRAMES)

    def capabilities(self):
        bars = [b for result in self.observations for b in result.get("bars", [])]
        observed = [b["timestamp_utc"] for b in bars]
        last = self.observations[-1] if self.observations else {}
        available = (last.get("status") == "AVAILABLE") if last else False if not self.authenticated else None
        return dict(provider=self.name, available=available, auth_present=self.authenticated,
            auth_required=self.auth_required, feed=self.feed, timeframes=list(self.supported_timeframes()),
            earliest_available=min(observed) if observed else None,
            latest_available=max(observed) if observed else None,
            history_depth="OBSERVED_WINDOWS_ONLY" if observed else "UNKNOWN_UNTESTED",
            history_status=self.history_status, premarket=True, afterhours=True,
            premarket_observed=any(b["session"] == "PREMARKET" for b in bars),
            afterhours_observed=any(b["session"] == "AFTERHOURS" for b in bars),
            coverage_by_timeframe={frame: dict(
                first=min((b["timestamp_utc"] for b in bars if b["timeframe"] == frame), default=None),
                last=max((b["timestamp_utc"] for b in bars if b["timeframe"] == frame), default=None))
                for frame in self.supported_timeframes()},
            split_adjustment=self.split_adjustment,
            rate_limit=dict(account_limit="UNKNOWN", client_requests_per_minute=60 / self.client.interval,
                            retries=self.client.retries, max_backoff_seconds=self.client.max_backoff),
            status=last.get("status", "NOT_TESTED" if self.authenticated else "DATA_UNAVAILABLE"),
            quality_status=last.get("quality", {}).get("status", "UNVERIFIED"),
            metadata=self.metadata(), as_of=dt.datetime.now(dt.timezone.utc).isoformat())

    def metadata(self):
        return dict(domain=self.client.host, timestamp="UTC interval start",
                    certification="NOT_HISTORICALLY_PIT_CERTIFIED", market_data_only=True)

    def coverage(self, request):
        """Observed request coverage only. No retention SLA inferred from docs."""
        matching = [r for r in self.observations if r["request"] == request.key(self.name, self.feed)]
        last = matching[-1] if matching else {}
        bars = last.get("bars", [])
        return dict(provider=self.name, feed=self.feed, status=last.get("status", "NOT_TESTED"),
                    first=min((b["timestamp_utc"] for b in bars), default=None),
                    last=max((b["timestamp_utc"] for b in bars), default=None), bars=len(bars))

    def preflight(self, request):
        return None if self.authenticated else "DATA_UNAVAILABLE"

    @abstractmethod
    def fetch_page(self, request, cursor):
        """Return (normalized bars, opaque next cursor). Must not mix feeds."""

    def fetch_bars(self, request):
        key = request.key(self.name, self.feed)
        state = dict(key=key, complete=False, pages=[], cursor=None, errors=[])
        bars, quarantined, cutoff = [], [], None
        # Cache integrity is independent of today's network entitlements and
        # retention window. Only an explicitly complete download bypasses them.
        if self.cache:
            state, bars = self.cache.load(key)
        cached = state["complete"]
        status = None if cached else self.preflight(request) or self.halted_status
        admitted = status is None
        event_start = len(self.client.events)
        if status is None:
            archived_events = list(state.get("http_events", []))
            try:
                if not cached and state.get("retry_not_before") and stamp(state["retry_not_before"]) > stamp(dt.datetime.now(dt.timezone.utc)):
                    admitted = False
                    raise ProviderError("RATE_LIMITED", 429)
                # A last page saved before a process interruption is finalized
                # without restarting pagination from its beginning.
                final_page_saved = bool(state["pages"] and state["cursor"] is None)
                visited = {str(state["cursor"])} if state["cursor"] else set()
                while not state["complete"] and not final_page_saved:
                    rows, cursor = self.fetch_page(request, state["cursor"])
                    if cursor is not None and str(cursor) in visited:
                        raise ProviderError("PAGINATION_LOOP")
                    if cursor is not None:
                        visited.add(str(cursor))
                    if len(state["pages"]) >= 10000:
                        raise ProviderError("PAGE_LIMIT_EXCEEDED")
                    # Providers may return inclusive endpoint bars; the internal
                    # interval is always [start, end). Never fill absent slots.
                    rows = [b for b in rows if stamp(request.start) <= stamp(b["timestamp_utc"]) < stamp(request.end)]
                    state["http_events"] = archived_events + self.client.events[event_start:]
                    if self.cache:
                        self.cache.page(key, state, rows, cursor)
                    else:
                        state["pages"].append(dict(bars=len(rows)))
                        state["cursor"] = cursor
                    bars.extend(rows)
                    final_page_saved = cursor is None
                had_bars = bool(bars)
                usable, quality, quarantined, cutoff = validated_observations(bars, request)
                state.update(complete=True, quality=quality)
                if self.cache:
                    self.cache.save(key, state)
                bars = usable
                status = "AVAILABLE" if bars else "INVALID_DATA" if had_bars else "EMPTY"
            except ProviderError as exc:
                status = exc.status
                state["http_events"] = archived_events + self.client.events[event_start:]
                if status in ("AUTH_ERROR", "RATE_LIMITED"):
                    # Stop this provider for this acquisition process. Other
                    # providers can still satisfy the whole bundle by fallback.
                    self.halted_status = status
                if exc.retry_after:
                    state["retry_not_before"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=exc.retry_after)).isoformat()
                state["errors"].append(dict(status=exc.status, http_status=exc.http_status,
                    at=dt.datetime.now(dt.timezone.utc).isoformat(), page=len(state["pages"])))
                if self.cache:
                    self.cache.save(key, state)
            except (KeyError, ValueError, TypeError, IndexError, OverflowError):
                status = "INVALID_RESPONSE"
                state["http_events"] = archived_events + self.client.events[event_start:]
                state["errors"].append(dict(status=status, page=len(state["pages"])))
                if self.cache:
                    self.cache.save(key, state)
        # A later pagination failure cannot erase a validated opening already
        # downloaded with valid prerequisites. Incomplete checkpoints without
        # those prerequisites never expose data through this path.
        partial = []
        if admitted and not state["complete"] and bars:
            partial, quality, quarantined, cutoff = validated_observations(bars, request)
            state["quality"] = quality
        result = dict(provider=self.name, feed=self.feed, request=key, status=status,
            bars=bars if status in ("AVAILABLE", "EMPTY", "INVALID_DATA") else [],
            partial_bars=len(bars) if status not in ("AVAILABLE", "EMPTY", "INVALID_DATA") else 0,
            quality=state.get("quality", dict(status="UNVERIFIED", errors=[], warnings=[])),
            quarantined_bars=quarantined,
            usable_bars=partial, validation_cutoff=cutoff, download_complete=state["complete"],
            cache_hit=cached, pages=len(state["pages"]), errors=state["errors"],
            http_events=self.client.events[event_start:], history_status=self.history_status,
            archived_http_events=state.get("http_events", []),
            metadata=self.metadata())
        self.observations.append(result)
        return result
