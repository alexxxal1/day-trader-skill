"""Configurable provider selection, pinned for an entire trade/session bundle."""
from dataclasses import replace

from common import stamp
from .alpaca import AlpacaProvider
from .cache import BarCache
from .massive import MassiveProvider
from .yahoo import YahooProvider

DEFAULT_PRIORITY = ("ALPACA", "YAHOO", "MASSIVE")
DEFAULT_ADJUSTMENTS = {"ALPACA": "raw", "YAHOO": "split", "MASSIVE": "raw"}


class ProviderRouter:
    def __init__(self, providers, priority=DEFAULT_PRIORITY, adjustments=None):
        self.providers = {p.name: p for p in providers}
        self.priority = tuple(priority)
        self.adjustments = dict(DEFAULT_ADJUSTMENTS, **(adjustments or {}))
        if len(set(priority)) != len(priority) or not priority or any(n not in self.providers for n in priority):
            raise ValueError("priority must contain unique registered providers")

    def fetch_trade(self, requests, opening=None, pinned_provider=None):
        """Never stitch. Fallback only on request availability/opening coverage,
        never on trigger, return, exit completeness or later execution outcome.
        """
        if not requests or len({(r.ticker, r.start, r.end) for r in requests}) != 1:
            raise ValueError("one ticker's complete trade window required")
        attempts = []
        if pinned_provider is not None and pinned_provider not in self.priority:
            raise ValueError("pinned provider must be registered in the frozen priority")
        for name in (pinned_provider,) if pinned_provider else self.priority:
            provider = self.providers[name]
            results = []
            for request in requests:
                result = provider.fetch_bars(replace(request, adjustment=self.adjustments[name]))
                if result.get("usable_bars"):
                    result = dict(result, bars=result["usable_bars"], download_status=result["status"], status="AVAILABLE_PREFIX")
                results.append(result)
            attempts.append(dict(provider=name, statuses=[r["status"] for r in results]))
            eligible = [r for r in results if r["status"] in ("AVAILABLE", "AVAILABLE_PREFIX") and
                any(not opening or b["timestamp_utc"] == opening for b in r["bars"])]
            if eligible:
                # Preserve native opening-based frame selection. Missing 1m
                # history or a later error in another frame does not discard
                # this provider's already valid opening observations.
                frames = {r["request"]["timeframe"] for r in eligible}
                frame = "1m" if "1m" in frames else "5m" if "5m" in frames else eligible[0]["request"]["timeframe"]
                return dict(status="AVAILABLE", provider=name, feed=provider.feed, results=results, attempts=attempts,
                    selection_basis=dict(rule="FIRST_PROVIDER_WITH_VALID_OPENING", opening=opening, timeframe=frame))
        return dict(status="DATA_UNAVAILABLE", provider=None, feed=None, results=[], attempts=attempts)


def configured(config, cache_root=None, sessions=None):
    cache = BarCache(cache_root or config.get("cache", "trader-backtest/intraday-cache"))
    providers = [AlpacaProvider(feed=config.get("alpaca_feed", "iex"), cache=cache, sessions=sessions),
                 YahooProvider(cache=cache, sessions=sessions), MassiveProvider(cache=cache, sessions=sessions)]
    for provider in providers:
        rpm = config.get("requests_per_minute", {}).get(provider.name)
        if rpm is not None:
            if type(rpm) not in (int, float) or rpm <= 0:
                raise ValueError("positive request rate required")
            provider.client.interval = 60 / rpm
    return ProviderRouter(providers, config.get("priority", DEFAULT_PRIORITY), config.get("adjustments"))
