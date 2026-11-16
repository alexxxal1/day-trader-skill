"""Massive aggregate bars and future as-of identity/reference access."""
import datetime as dt
import os
import urllib.parse

from common import iso, stamp
from .base import IntradayProvider
from .model import TIMEFRAMES, normalize_bar
from .transport import HTTPClient, ProviderError


class MassiveProvider(IntradayProvider):
    name, feed = "MASSIVE", "US_STOCKS_AGGREGATES"

    def __init__(self, client=None, cache=None, sessions=None):
        self._key = os.environ.get("MASSIVE_API_KEY")
        # Conservative local pacing, not a claim about the account's entitlement.
        super().__init__(client or HTTPClient("api.massive.com", requests_per_minute=5), cache, sessions)

    @property
    def authenticated(self):
        return bool(self._key)

    def headers(self):
        return {"Authorization": "Bearer " + self._key}

    def next_url(self, url):
        if not url:
            return None
        parts = urllib.parse.urlsplit(url)
        allowed = ("/v2/aggs/", "/v3/reference/", "/vX/reference/", "/stocks/v1/splits")
        if parts.scheme != "https" or parts.netloc != "api.massive.com" or not parts.path.startswith(allowed) or parts.fragment:
            raise ProviderError("UNSAFE_PAGINATION_URL")
        # Provider continuations can contain apiKey. Never put it on disk.
        query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k.lower() not in ("apikey", "api_key")]
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), ""))

    def fetch_page(self, request, cursor):
        start = int(stamp(request.start).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
        end = int(stamp(request.end).replace(tzinfo=dt.timezone.utc).timestamp() * 1000) - 1
        url = cursor or "https://api.massive.com/v2/aggs/ticker/%s/range/%d/minute/%d/%d" % (
            urllib.parse.quote(request.ticker, safe=""), TIMEFRAMES[request.timeframe], start, end)
        params = {} if cursor else dict(adjusted=str(request.adjustment == "split").lower(), sort="asc", limit=50000)
        body = self.client.get(url, params, self.headers())
        if body.get("status") not in ("OK", "DELAYED"):
            raise ProviderError("PROVIDER_ERROR")
        if body.get("ticker", request.ticker) != request.ticker:
            raise ProviderError("IDENTITY_MISMATCH")
        if "adjusted" in body and body["adjusted"] != (request.adjustment == "split"):
            raise ProviderError("ADJUSTMENT_MISMATCH")
        rows = body.get("results") or []
        return [normalize_bar(request, iso(dt.datetime.fromtimestamp(r["t"] / 1000, dt.timezone.utc)), r,
                    self.name, self.feed, self.sessions) for r in rows], self.next_url(body.get("next_url"))

    def reference(self, path, params):
        """Prepared only; never called automatically by acquisition or strategy."""
        if not self.authenticated:
            return dict(status="DATA_UNAVAILABLE", results=[])
        records, url, visited = [], "https://api.massive.com" + path, set()
        try:
            while url:
                if url in visited or len(visited) >= 1000:
                    raise ProviderError("PAGINATION_LOOP")
                visited.add(url)
                body = self.client.get(url, params, self.headers())
                if body.get("status") != "OK":
                    raise ProviderError("PROVIDER_ERROR")
                result = body.get("results") or []
                records.extend(result if isinstance(result, list) else [result])
                url, params = self.next_url(body.get("next_url")), {}
            return dict(status="AVAILABLE" if records else "EMPTY", results=records,
                        provider=self.name, historical_certification="REFERENCE_SNAPSHOT_NOT_PIT_CERTIFIED")
        except ProviderError as exc:
            return dict(status=exc.status, results=[], partial_records=len(records))

    def tickers(self, active=True, as_of=None):
        params = dict(market="stocks", locale="us", active=str(active).lower(), limit=1000)
        if as_of:
            params["date"] = dt.date.fromisoformat(as_of).isoformat()
        return self.reference("/v3/reference/tickers", params)

    def ticker_details(self, ticker, as_of):
        return self.reference("/v3/reference/tickers/" + urllib.parse.quote(ticker, safe=""),
                              dict(date=dt.date.fromisoformat(as_of).isoformat()))

    def splits(self, ticker):
        return self.reference("/stocks/v1/splits", dict(ticker=ticker, limit=1000))

    def ticker_events(self, ticker):
        # Experimental endpoint, exposed without assuming plan availability.
        return self.reference("/vX/reference/tickers/" + urllib.parse.quote(ticker, safe="") + "/events", {})
