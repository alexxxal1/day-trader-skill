"""Wrap the retained Yahoo chart acquisition without evading its limits."""
import datetime as dt
import json
from pathlib import Path
import tempfile

from common import digest, iso, stamp
from intraday_audit import describe, request_chart
from .base import IntradayProvider
from .model import normalize_bar
from .transport import HTTPClient, ProviderError


class YahooProvider(IntradayProvider):
    name, feed = "YAHOO", "UNSPECIFIED"
    auth_required = False
    history_status = "INTRADAY_HISTORY_LIMITED"

    def __init__(self, cache=None, sessions=None, requester=None, now=None):
        self.requester = requester or request_chart
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        # Retained Yahoo policy: one attempt, no retries/endpoint rotation.
        super().__init__(HTTPClient("query2.finance.yahoo.com", retries=0, requests_per_minute=120), cache, sessions)

    @property
    def authenticated(self):
        return False

    def preflight(self, request):
        if request.adjustment != "split":
            return "UNSUPPORTED_ADJUSTMENT"
        days = {"1m": 7, "5m": 60, "15m": 60, "60m": 730}[request.timeframe]
        if stamp(request.start) < stamp(self.now()) - dt.timedelta(days=days):
            return "INTRADAY_HISTORY_LIMITED"
        if stamp(request.end) - stamp(request.start) > dt.timedelta(days=days):
            return "INTRADAY_HISTORY_LIMITED"
        return None

    def fetch_page(self, request, cursor):
        params = dict(interval=request.timeframe, period1=int(stamp(request.start).replace(tzinfo=dt.timezone.utc).timestamp()),
            period2=int(stamp(request.end).replace(tzinfo=dt.timezone.utc).timestamp()), includePrePost="true", events="splits,div")
        # Keep request_chart's immutable raw archive and error/429 semantics.
        if self.client.last_call is not None:
            self.client.sleep(max(0, self.client.interval - (self.client.monotonic() - self.client.last_call)))
        self.client.last_call = self.client.monotonic()
        with tempfile.TemporaryDirectory() as temporary:
            root = self.cache.directory(request.key(self.name, self.feed)) if self.cache else Path(temporary)
            record, body = self.requester(request.ticker, params, root / "yahoo-raw.json.gz")
        status = record.get("http_status")
        self.client.events.append(dict(http_status=status, attempt=1))
        if status in (401, 403):
            raise ProviderError("AUTH_ERROR", status)
        if status == 429:
            raise ProviderError("RATE_LIMITED", status)
        if status != 200 or describe(body, request.timeframe)["status"] not in ("AVAILABLE", "EMPTY"):
            raise ProviderError("PROVIDER_ERROR", status)
        result = json.loads(body)["chart"]["result"][0]
        meta = result["meta"]
        granularity = meta.get("dataGranularity")
        if meta.get("symbol", "").upper() != request.ticker.replace(".", "-") or (
                granularity != request.timeframe and (request.timeframe, granularity) != ("60m", "1h")):
            raise ProviderError("IDENTITY_MISMATCH")
        quotes = result["indicators"]["quote"][0]
        rows = []
        for i, t in enumerate(result.get("timestamp") or []):
            values = {k: (quotes.get(col) or [])[i] if i < len(quotes.get(col) or []) else None
                      for k, col in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume"))}
            rows.append(normalize_bar(request, iso(dt.datetime.fromtimestamp(t, dt.timezone.utc)), values, self.name, self.feed, self.sessions))
        return rows, None

    def capabilities(self):
        result = super().capabilities()
        if not self.observations:
            result.update(available=None, status="NOT_TESTED")
        return result
