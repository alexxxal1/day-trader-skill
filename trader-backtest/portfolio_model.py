"""Portfolio contracts and USD-quote/EUR-account valuation. Offline only."""
import bisect
import math

from common import EPOCH, module, stamp
from engine import friction

RISK = module("trader-risk", "check")
EXPOSURE = module("trader-risk", "exposure")
RANKING = ["trigger_setup_status", "RS_PROXY", "liquidity", "symbol", "id"]


def positive(value, name, zero=False):
    if type(value) not in (int, float) or not math.isfinite(value) or (value < 0 if zero else value <= 0):
        raise ValueError("invalid " + name)
    return value


def validate(config):
    positive(config["initial_equity"], "initial_equity")
    if config["account_currency"] not in ("EUR", "USD") or type(config["leverage"]) is not int or config["leverage"] != 1:
        raise ValueError("base simulator supports EUR/USD accounts and leverage=1 only")
    if config["ranking"] != RANKING:
        raise ValueError("predeclared ranking must not change")
    for k in ("max_positions", "paths", "block_sessions", "forward_horizon"):
        if type(config[k]) is not int or config[k] <= 0:
            raise ValueError("positive integer required: " + k)
    if type(config["seed"]) is not int or config["seed"] < 0:
        raise ValueError("nonnegative integer seed required")
    positive(config["max_portfolio_risk"], "max_portfolio_risk")
    if config["max_portfolio_risk"] > 1:
        raise ValueError("invalid portfolio risk")
    if not config["risk_per_trade"]:
        raise ValueError("at least one risk scenario required")
    for value in config["risk_per_trade"]:
        if not 0 < positive(value, "risk") <= config["max_portfolio_risk"]:
            raise ValueError("risk exceeds portfolio limit")
    if not config["cost_scenarios"] or set(config["cost_scenarios"]) - {"IDEAL", "REALISTIC", "STRESS"}:
        raise ValueError("unknown costs")
    if not config["event_variants"] or set(config["event_variants"]) - {"FULL_RESET", "COOLDOWN20"}:
        raise ValueError("unknown event variant")
    if config["primary_variant"] not in config["event_variants"]:
        raise ValueError("primary variant must be included")
    if not config["strategies"] or set(config["strategies"]) - {"MINERVINI", "BREAKOUT_SETUP_READY", "CONFLUENCE_SETUP_READY"}:
        raise ValueError("unknown strategy")
    rules = config.get("risk_rules", {})
    allowed = {"cash_buffer_pct", "max_position_pct", "max_order_usd", "min_order_usd", "min_reward_risk",
               "max_daily_loss_usd", "max_trades_per_day", "max_orders_per_day", "blocked",
               "no_entries_before", "no_entries_after", "allow_short", "allow_margin"}
    if set(rules) - allowed:
        raise ValueError("unsupported historical risk rule")
    for key, value in rules.items():
        if key not in {"blocked", "no_entries_before", "no_entries_after", "allow_short", "allow_margin"}:
            positive(value, key, zero=True)
    merged = dict(RISK.DEFAULTS, **rules)
    for key in ("max_trades_per_day", "max_orders_per_day"):
        if type(merged[key]) is not int or merged[key] < 1:
            raise ValueError("positive integer required: " + key)
    for key in ("allow_short", "allow_margin"):
        if type(merged[key]) is not bool:
            raise ValueError("boolean required: " + key)
    if not 0 <= merged["cash_buffer_pct"] < 100 or not 0 < merged["max_position_pct"] <= 100:
        raise ValueError("invalid cash/position percentage")
    return merged


class FX:
    """USD per account unit. Never infer EUR=USD or use an unfinished FX bar.

    Yahoo FX daily timestamps identify the start of a UTC day. Its daily close
    is conservatively available 24 hours later; last completed observation is
    used for each stock quote. Weekend carry is allowed for at most four days.
    """
    def __init__(self, currency, result=None):
        self.currency, self.times, self.rates = currency, [], []
        if currency == "USD":
            return
        if currency != "EUR" or result is None or result.get("meta", {}).get("symbol") != "EURUSD=X":
            raise ValueError("EUR accounts require an explicit EURUSD=X archive")
        if result["meta"].get("dataGranularity") != "1d" or result["meta"].get("currency") != "USD":
            raise ValueError("FX archive must contain daily USD-per-EUR quotes")
        closes = result["indicators"]["quote"][0]["close"]
        for t, close in zip(result["timestamp"], closes):
            if type(close) in (int, float) and math.isfinite(close) and close > 0:
                self.times.append(t + 86400)
                self.rates.append(close)
        if self.times != sorted(set(self.times)):
            raise ValueError("FX archive must have unique sorted timestamps")

    def at(self, when):
        if self.currency == "USD":
            return 1.0
        now = (stamp(when) - EPOCH).total_seconds() if isinstance(when, str) else (when - EPOCH).total_seconds()
        i = bisect.bisect_right(self.times, now) - 1
        if i < 0 or now - self.times[i] > 4 * 86400:
            raise ValueError("MISSING_OR_STALE_HISTORICAL_FX")
        return self.rates[i]


def unit_values(mid, stop, target, side, fx, costs):
    """Same spread/round-trip cost convention as trader-risk, at both fills."""
    for name, value in (("entry", mid), ("stop", stop), ("target", target), ("fx", fx)):
        positive(value, name)
    if side not in ("long", "short"):
        raise ValueError("unknown direction")
    for key in ("spread_bps", "slippage_bps_per_side", "commission_per_unit_roundtrip"):
        positive(costs[key], key, zero=True)
    sign = 1 if side == "long" else -1
    entry = mid + sign * friction(mid, costs)
    stop_fill = stop - sign * friction(stop, costs)
    target_fill = target - sign * friction(target, costs)
    if sign * (mid - stop) <= 0 or sign * (target_fill - entry) <= 0:
        raise ValueError("OPEN_OUTSIDE_BRACKET")
    # The shared risk module validates direction and round-trip allowances.
    basis = (mid + stop) / 2
    terms = EXPOSURE.terms({"symbol": "BACKTEST", "side": side,
        "spread": basis * costs["spread_bps"] / 10000,
        "slippage": basis * 2 * costs["slippage_bps_per_side"] / 10000,
        "commission_per_unit": costs["commission_per_unit_roundtrip"]})
    risk_usd = sign * (mid - stop) + float(terms["cost"])
    fee = costs["commission_per_unit_roundtrip"] / 2
    return {"entry": entry, "fee": fee / fx, "risk": risk_usd / fx,
            "reward_risk": (sign * (target_fill - entry) - 2 * fee) / risk_usd,
            "cash": (entry + 2 * fee) / fx if side == "long" else max(mid, stop) * 1.5 / fx + risk_usd / fx,
            "entry_drag": (abs(entry - mid) + fee) / fx, "stop_fill": stop_fill}


def position_size(equity, cash, used_risk, mid, stop, target, side, fx, costs, config, risk):
    rules = validate(config)
    values = unit_values(mid, stop, target, side, fx, costs)
    if not 0 < positive(risk, "risk") <= config["max_portfolio_risk"]:
        raise ValueError("risk exceeds portfolio limit")
    if equity <= 0:
        return 0, values
    # Admission includes entry friction/fees so the aggregate budget also holds
    # immediately after entry. Later gaps/FX/mark losses can exceed planned risk.
    aggregate = (equity * config["max_portfolio_risk"] - used_risk) / (
        values["risk"] + values["entry_drag"] * config["max_portfolio_risk"])
    caps = [equity * risk / values["risk"], aggregate,
            max(0, cash - equity * rules["cash_buffer_pct"] / 100) / values["cash"],
            equity * rules["max_position_pct"] / 100 / (mid / fx), rules["max_order_usd"] / mid]
    return max(0, math.floor(min(caps) + 1e-10)), values
