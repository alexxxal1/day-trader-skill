"""Official Alpaca historical STOCK market data, no broker API dependency."""
import os
import urllib.parse

from .base import IntradayProvider
from .model import TIMEFRAMES, normalize_bar
from .transport import HTTPClient, ProviderError


class AlpacaProvider(IntradayProvider):
    name = "ALPACA"

    def __init__(self, feed="iex", client=None, cache=None, sessions=None, page_size=10000):
        feed = feed.lower()
        if feed not in ("iex", "sip") or not 1 <= page_size <= 10000:
            raise ValueError("Alpaca requires explicit iex/sip and page size 1..10000")
        self.feed, self.page_size = feed.upper(), page_size
        self._key, self._secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_API_SECRET")
        super().__init__(client or HTTPClient("data.alpaca.markets"), cache, sessions)

    @property
    def authenticated(self):
        return bool(self._key and self._secret)

    def fetch_page(self, request, cursor):
        url = "https://data.alpaca.markets/v2/stocks/" + urllib.parse.quote(request.ticker, safe="") + "/bars"
        # Official REST accepts minute multipliers 1..59; a 60-minute bar is
        # transmitted as 1Hour. The neutral 60m/60Min request remains supported.
        timeframe = "1Hour" if request.timeframe == "60m" else str(TIMEFRAMES[request.timeframe]) + "Min"
        params = dict(timeframe=timeframe, start=request.start,
            end=request.end, adjustment=request.adjustment, feed=self.feed.lower(), sort="asc", limit=self.page_size)
        if cursor:
            params["page_token"] = cursor
        body = self.client.get(url, params, {"APCA-API-KEY-ID": self._key, "APCA-API-SECRET-KEY": self._secret})
        if "bars" not in body:
            raise ProviderError("INVALID_RESPONSE")
        rows = body["bars"] or []
        if not isinstance(rows, list):
            raise ProviderError("INVALID_RESPONSE")
        cursor = body.get("next_page_token")
        if cursor is not None and not isinstance(cursor, str):
            raise ProviderError("INVALID_RESPONSE")
        return [normalize_bar(request, r["t"], r, self.name, self.feed, self.sessions) for r in rows], cursor or None

    def metadata(self):
        return dict(super().metadata(), feed_scope="IEX exchange only" if self.feed == "IEX" else
                    "SIP consolidated; separate entitlement required", price_basis="REQUESTED_RAW_OR_SPLIT")
