"""Generic future challenge extension. Never part of the main portfolio run."""
import math
import datetime as dt


def first_hit(condition):
    """Explicit no-hit sentinel: False-only series must never become index zero."""
    return next((i for i, reached in enumerate(condition) if bool(reached)), None)


def evaluate_challenge(observations, initial, rules):
    """Ordered equity observations with day and optional entry risk fraction.

    Loss barriers are inclusive and take precedence on simultaneous hits.
    Total loss is measured from initial equity, daily loss from that day's first
    observation, optional trailing loss from the running peak. Rules are generic
    fractions of initial/day-opening/peak equity, with no firm-specific defaults.
    Supply the day-opening observation even on days with no entries.
    """
    if not math.isfinite(initial) or initial <= 0:
        raise ValueError("positive initial equity required")
    allowed = {"profit_target", "maximum_total_loss", "maximum_daily_loss", "trailing_drawdown",
               "minimum_trading_days", "maximum_risk_per_trade", "challenge_period_days"}
    if set(rules) - allowed:
        raise ValueError("unknown challenge rule")
    for key, value in rules.items():
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
            raise ValueError("positive challenge parameter required: " + key)
        if value is not None and key in {"maximum_total_loss", "maximum_daily_loss", "trailing_drawdown", "maximum_risk_per_trade"} and value > 1:
            raise ValueError("loss/risk fractions must be <= 1")
        if value is not None and key in {"minimum_trading_days", "challenge_period_days"} and type(value) is not int:
            raise ValueError("integer day count required")
    if any(a["day"] > b["day"] for a,b in zip(observations,observations[1:])):
        raise ValueError("observations must be chronological")
    target, loss, peak, days, traded = [], [], initial, {}, set()
    for row in observations:
        equity = row["equity"]
        if type(equity) not in (int, float) or not math.isfinite(equity):
            raise ValueError("finite equity observations required")
        days.setdefault(row["day"], equity)
        peak = max(peak, equity)
        if row.get("is_entry", False):
            if rules.get("maximum_risk_per_trade") is not None and (type(row.get("risk_per_trade")) not in (int,float) or
                    not math.isfinite(row["risk_per_trade"]) or row["risk_per_trade"] < 0):
                raise ValueError("entry risk required for a maximum-risk challenge rule")
            traded.add(row["day"])
        hit_loss = any((rules.get("maximum_total_loss") is not None and equity <= initial * (1 - rules["maximum_total_loss"]),
            rules.get("maximum_daily_loss") is not None and equity <= days[row["day"]] * (1 - rules["maximum_daily_loss"]),
            rules.get("trailing_drawdown") is not None and equity <= peak * (1 - rules["trailing_drawdown"]),
            rules.get("maximum_risk_per_trade") is not None and row.get("risk_per_trade", 0) > rules["maximum_risk_per_trade"]))
        eligible = len(traded) >= rules.get("minimum_trading_days", 0)
        elapsed = (dt.date.fromisoformat(row["day"]) - dt.date.fromisoformat(observations[0]["day"])).days + 1
        in_time = rules.get("challenge_period_days") is None or elapsed <= rules["challenge_period_days"]
        threshold = initial * (1 + rules["profit_target"]) if rules.get("profit_target") is not None else None
        reached = threshold is not None and (equity >= threshold or math.isclose(equity,threshold,rel_tol=1e-12))
        target.append(bool(in_time and eligible and reached))
        loss.append(bool(hit_loss and in_time))
    target_at, loss_at = first_hit(target), first_hit(loss)
    status = "NO_HIT"
    if loss_at is not None and (target_at is None or loss_at <= target_at):
        status = "LOSS_FIRST"
    elif target_at is not None:
        status = "TARGET_FIRST"
    return {"status": status, "profit_target_hit": target_at is not None, "loss_limit_hit": loss_at is not None,
            "profit_target_index": target_at, "loss_limit_index": loss_at,
            "tie_policy": "Loss first on the same observation; bar paths do not prove intrabar ordering"}
