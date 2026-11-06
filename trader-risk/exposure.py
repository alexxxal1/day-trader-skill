"""Shared, offline valuation for paper stock/CFD plans. Never a broker payload.

Prices and stops use one mid-price basis. A full round-trip spread plus the
explicit cost allowances is charged once. Leverage changes collateral only,
never the price-distance loss or the notional position limits.
"""

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import re


class ExposureError(ValueError):
    pass


def decimal(value, label, minimum=0, strict=False):
    try:
        if isinstance(value, bool):
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or (result <= minimum if strict else result < minimum):
            raise ValueError()
        return result
    except (ValueError, InvalidOperation, TypeError):
        raise ExposureError("%s must be a finite number %s %s" % (label, ">" if strict else ">=", minimum))


def side(value):
    result = {"long": "buy", "short": "sell", "buy": "buy", "sell": "sell"}.get(str(value).lower())
    if result is None:
        raise ExposureError("side must be buy/long or sell/short")
    return result


def extended(plan):
    return (plan.get("analysis_only") is True or plan.get("side", "buy") in ("sell", "short")
            or plan.get("instrument_type", "stock") != "stock"
            or any(key in plan for key in ("spread", "contract", "leverage")))


def terms(plan):
    """Validate contract identity and valuation data, without guessing CFD terms."""
    kind = plan.get("instrument_type", "stock")
    if not isinstance(plan.get("symbol"), str) or not plan["symbol"].strip():
        raise ExposureError("an exact instrument symbol is required")
    if kind not in ("stock", "cfd"):
        raise ExposureError("instrument_type not supported: only stock and stock CFD, no options or futures")
    if plan.get("currency", "USD" if kind == "stock" else None) != "USD":
        raise ExposureError("only USD price and account currency supported; no inferred FX conversion")
    direction = side(plan.get("side", "buy"))
    leverage = decimal(plan.get("leverage", 1), "leverage", 1)
    spread = decimal(plan.get("spread", 0) if kind == "stock" else plan.get("spread"), "spread")
    if plan.get("price_basis", "mid" if kind == "stock" else None) != "mid":
        raise ExposureError("price_basis must be mid; executable bid/ask prices would double-count spread")
    spec = plan.get("contract") or {}
    if kind == "cfd":
        if not isinstance(spec, dict) or spec.get("verified") is not True or not spec.get("source"):
            raise ExposureError("CFD contract needs verified terms and source")
        if spec.get("underlying_type") != "stock" or not spec.get("underlying_symbol") or spec.get("calendar") != "NYSE":
            raise ExposureError("CFD underlying must be an identified USD stock on the NYSE session calendar")
        if not re.fullmatch(r"[A-Z]{1,5}([.\-][A-Z]{1,2})?", str(spec["underlying_symbol"])):
            raise ExposureError("CFD underlying_symbol must identify a stock, not an option or future")
        if not isinstance(spec.get("guaranteed_stop"), bool):
            raise ExposureError("CFD guaranteed_stop must be explicitly true or false")
        size = decimal(spec.get("contract_size"), "CFD contract_size", strict=True)
        step = decimal(spec.get("qty_step"), "CFD qty_step", strict=True)
        minimum = decimal(spec.get("min_qty"), "CFD min_qty", strict=True)
        tick = decimal(spec.get("tick_size"), "CFD tick_size", strict=True)
        margin = decimal(spec.get("margin_rate"), "CFD margin_rate", strict=True)
        if margin > 1:
            raise ExposureError("CFD margin_rate must be <= 1")
        if "leverage" not in plan:
            leverage = 1 / margin
        if abs(leverage * margin - 1) > Decimal("0.000001"):
            raise ExposureError("CFD leverage and margin_rate disagree")
        commission = decimal(spec.get("commission_per_contract"), "CFD commission_per_contract")
        financing = decimal(spec.get("financing_reserve_per_contract"), "CFD financing_reserve_per_contract")
        slippage = decimal(spec.get("slippage"), "CFD slippage")
        guarantee_fee = decimal(spec.get("guaranteed_stop_fee_per_contract"), "CFD guaranteed_stop_fee_per_contract")
    else:
        size = step = minimum = Decimal(1)
        tick = Decimal("0.01")
        margin = decimal(plan.get("short_margin_rate", "1.5"), "short_margin_rate", strict=True) if direction == "sell" else 1 / leverage
        if direction == "sell" and leverage != 1:
            raise ExposureError("stock shorts use short_margin_rate, not a synthetic leverage ratio; use verified CFD terms for leveraged short contracts")
        commission = decimal(plan.get("commission_per_unit", 0), "commission_per_unit")
        financing = decimal(plan.get("financing_reserve_per_unit", 0), "financing_reserve_per_unit")
        slippage = decimal(plan.get("slippage", 0), "slippage")
        guarantee_fee = Decimal(0)
    if minimum % step:
        raise ExposureError("min_qty must be a multiple of qty_step")
    return {"kind": kind, "side": direction, "size": size, "step": step, "minimum": minimum,
            "tick": tick, "margin": margin, "leverage": leverage, "spread": spread,
            "cost": (spread + slippage) * size + commission + financing + guarantee_fee,
            "guaranteed": kind == "cfd" and spec["guaranteed_stop"], "financing": financing}


def permissions(plan, cfg):
    t = terms(plan)
    if cfg.get("paper", True) is not True:
        raise ExposureError("extended analysis requires paper=true; live short/CFD trading is not supported")
    if t["side"] == "sell" and cfg.get("allow_short") is not True:
        raise ExposureError("short analysis requires explicit allow_short=true")
    if t["kind"] == "cfd" and cfg.get("allow_cfd") is not True:
        raise ExposureError("CFD analysis requires explicit allow_cfd=true")
    if (t["kind"] == "cfd" or t["leverage"] > 1 or t["side"] == "sell") and cfg.get("allow_margin") is not True:
        raise ExposureError("margin analysis requires explicit allow_margin=true")
    return t


def metrics(plan, entry=None, qty=1):
    t = terms(plan)
    e = decimal(plan.get("entry") if entry is None else entry, "entry", strict=True)
    stop = decimal(plan.get("stop"), "stop", strict=True)
    target = decimal(plan.get("target"), "target", strict=True)
    q = decimal(qty, "qty", strict=True)
    if q < t["minimum"] or q % t["step"]:
        raise ExposureError("qty must meet min_qty and qty_step")
    if any(value % t["tick"] for value in (e, stop, target)):
        raise ExposureError("entry, stop and target must align with tick_size")
    sign = Decimal(1) if t["side"] == "buy" else Decimal(-1)
    distance, reward = sign * (e - stop), sign * (target - e)
    if distance <= 0 or reward <= 0:
        raise ExposureError("invalid stop/target: long needs stop < entry < target; short needs target < entry < stop")
    risk = distance * t["size"] + t["cost"]
    net_reward = reward * t["size"] - t["cost"]
    notional = e * t["size"]
    # Reserve collateral at the adverse stop price as well as the planned loss.
    margin = max(e, stop) * t["size"] * t["margin"]
    reserve = notional + t["cost"] if t["kind"] == "stock" and t["side"] == "buy" and t["leverage"] == 1 else margin + risk
    loss = q * risk
    maximum = loss if t["guaranteed"] else None
    if t["kind"] == "stock" and t["side"] == "buy":
        maximum = q * (notional + t["cost"])
    return {"terms": t, "entry": e, "stop": stop, "target": target, "qty": q,
            "risk_per_unit": risk, "reward_per_unit": net_reward, "reward_risk": net_reward / risk,
            "notional_per_unit": notional, "margin_per_unit": margin, "cash_per_unit": reserve,
            "loss_at_stop": loss, "maximum_possible_loss": maximum}


def usd(value):
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_CEILING))


def summary(m):
    t, q = m["terms"], m["qty"]
    return {"analysis_only": True, "paper": True, "instrument_type": t["kind"],
            "leverage": float(t["leverage"]), "margin_rate": float(t["margin"]),
            "notional_usd": usd(q * m["notional_per_unit"]), "margin_required_usd": usd(q * m["margin_per_unit"]),
            "cash_required_usd": usd(q * m["cash_per_unit"]), "costs_usd": usd(q * t["cost"]),
            "loss_at_stop_usd": usd(m["loss_at_stop"]),
            "maximum_possible_loss_usd": usd(m["maximum_possible_loss"]) if m["maximum_possible_loss"] is not None else None,
            "loss_bound_note": ("Guaranteed stop per verified CFD terms, including modelled costs." if t["guaranteed"] else
                                "Stop loss is not a guaranteed cap. Short stock and unguaranteed CFD losses have no verified per-trade cap." if m["maximum_possible_loss"] is None else
                                "Long stock can lose its entire notional plus modelled costs; the stop is not guaranteed."),
            "cost_notes": ["Financing/overnight and stock-borrow costs may apply; allowances are estimates, not guaranteed caps.",
                           "Higher leverage reduces collateral, not the loss for the same exposure or the notional limits."]}


def size_plan(plan, cfg, budget=None, limit=None, price=None, cash=None, today=None):
    t = permissions(plan, cfg)
    if today and plan.get("date") != today:
        raise ExposureError("make a new plan: plan date must equal today's New York date")
    if not plan.get("symbol") or str(plan["symbol"]).upper() in {str(s).upper() for s in cfg.get("blocked", [])}:
        raise ExposureError("missing symbol or symbol on blocked list")
    budget = decimal(budget if budget is not None else cfg.get("budget"), "budget", strict=True)
    buffer_pct = decimal(cfg.get("cash_buffer_pct", 10), "cash_buffer_pct")
    if buffer_pct >= 100:
        raise ExposureError("cash_buffer_pct must be under 100")
    e = decimal(plan.get("entry"), "entry", strict=True)
    source = "plan"
    if limit is not None:
        e, source = decimal(limit, "limit", strict=True), "limit"
    elif price is not None:
        p = decimal(price, "price", strict=True)
        if (t["side"] == "buy" and p > e) or (t["side"] == "sell" and p < e):
            e, source = p, "price"
    # A unit below min_qty is sufficient for per-unit valuation.
    unit = metrics(plan, e, max(t["minimum"], t["step"]))
    buffer = budget * buffer_pct / 100
    free = max(Decimal(0), min(budget, decimal(cash, "cash") if cash is not None else budget) - buffer)
    caps = [("max loss", decimal(cfg.get("max_loss_per_trade_usd"), "max_loss_per_trade_usd", strict=True), unit["risk_per_unit"]),
            ("position cap", budget * decimal(cfg.get("max_position_pct", 75), "max_position_pct", strict=True) / 100, unit["notional_per_unit"]),
            ("max order", decimal(cfg.get("max_order_usd", 4000), "max_order_usd", strict=True), unit["notional_per_unit"]),
            ("cash", free, unit["cash_per_unit"])]
    limits = [{"name": name, "qty": float((amount / per / t["step"]).to_integral_value(rounding=ROUND_FLOOR) * t["step"]),
               "usd": float(amount)} for name, amount, per in caps]
    q = min(Decimal(str(item["qty"])) for item in limits)
    bound = next(item["name"] for item in limits if Decimal(str(item["qty"])) == q)
    refusal = None
    if q < t["minimum"]:
        refusal = "quantity below min_qty: %s limit; no paper proposal" % bound
    elif unit["reward_risk"] < decimal(cfg.get("min_reward_risk", 1), "min_reward_risk"):
        refusal = "reward:risk below minimum after spread and costs"
    elif q * unit["notional_per_unit"] < decimal(cfg.get("min_order_usd", 5), "min_order_usd"):
        refusal = "notional below minimum order"
    result = dict(plan, side=t["side"], analysis_only=True, paper=True, qty=float(q), entry=float(e),
                  entry_plan=plan["entry"], entry_from=source, limits=limits, bound_by=bound,
                  refusal=refusal, order_type="limit" if limit is not None else "market",
                  risk_per_share=float(unit["risk_per_unit"]), reward_risk=float(unit["reward_risk"]))
    if q > 0:
        result.update(summary(metrics(plan, e, q)))
        result["max_loss"] = result["loss_at_stop_usd"]
    return result


def proposal(sizing):
    if sizing["refusal"] or sizing["qty"] <= 0:
        return None
    # Deliberately not place_stock_order arguments. check.py validates this
    # paper proposal and uses a fingerprint incompatible with trader-broker.
    return {"analysis_only": True, "paper": True, "symbol": sizing["symbol"], "side": sizing["side"],
            "instrument_type": sizing.get("instrument_type", "stock"), "qty": str(sizing["qty"]),
            "type": sizing["order_type"], "limit_price": sizing["entry"] if sizing["order_type"] == "limit" else None,
            "plan": {k: v for k, v in sizing.items() if k not in ("limits", "refusal")}}
