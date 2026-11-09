"""Explicit coupon scenarios and independently confirmed current accrual periods.

No rates, dates, amounts or issuer rules are inferred here. Confirmations are
bound to the reviewed inputs so an edited schedule cannot reuse an old consent.
"""
from __future__ import annotations

import copy
import math
from datetime import date

from models import Bond, parse_date

SCENARIO_REQUIRED = "Необходим сценарий будущих купонов"
SCENARIO_LABEL = "Расчёт по заданным выплатам"
SCENARIO_NOTE = ("Расчёт выполнен при неизменных сценарных денежных потоках. Изменение будущих купонов "
                 "вследствие изменения рыночных ставок не моделируется. Дюрация не является полной оценкой "
                 "чувствительности плавающей облигации к изменению базовой ставки (например, TONIA).")


def requires_coupon_scenario(bond: Bond) -> bool:
    return bond.coupon_type in ("floating", "unknown")


def coupon_scenario_signature(bond: Bond) -> dict:
    signature = {"coupon_type": bond.coupon_type,
            "valuation_date": bond.valuation_date.isoformat() if bond.valuation_date else None,
            "maturity_date": bond.maturity_date.isoformat() if bond.maturity_date else None,
            "face_value": bond.face_value, "coupon_frequency": bond.coupon_frequency,
            "nonstandard_coupon_period": bond.nonstandard_coupon_period,
            "known_dates": [d.isoformat() for d in bond.user_supplied_coupon_dates],
            "cashflows": [{"payment_date": f.payment_date.isoformat(),
                           "coupon_amount": f.coupon_amount,
                           "redemption_amount": f.redemption_amount}
                          for f in bond.cashflows]}
    return signature


def _legacy_coupon_scenario_signature(bond: Bond) -> dict:
    """Signature used by saved scenarios before technical origins were added."""
    signature = {"isin": bond.isin.strip().upper(), "coupon_type": bond.coupon_type,
        "valuation_date": bond.valuation_date.isoformat() if bond.valuation_date else None,
        "maturity_date": bond.maturity_date.isoformat() if bond.maturity_date else None,
        "face_value": bond.face_value, "coupon_frequency": bond.coupon_frequency,
        "schedule_source": bond.schedule_source,
        "nonstandard_coupon_period": bond.nonstandard_coupon_period,
        "known_dates": [day.isoformat() for day in bond.user_supplied_coupon_dates],
        "cashflows": [flow.to_dict() for flow in bond.cashflows]}
    if bond.is_model:
        signature["position_key"] = bond.position_key
    return signature


def coupon_scenario_confirmed(bond: Bond) -> bool:
    meta = bond.provenance.get("coupon_scenario", {})
    try:
        checked = parse_date(meta.get("checked_on"))
    except (ValueError, TypeError):
        return False
    # A scenario is a user-defined set of future amounts, never a verified
    # market forecast. Confirmation records only that the user reviewed its
    # completeness; it does not require or imply an external source.
    common = bond.schedule_kind == "scenario" and bond.schedule_confirmed and bool(checked)
    if not common:
        return False
    # New records distinguish internal creation paths from external data
    # sources. Also accept the source-free intermediate schema created in the
    # preceding release and the older signed schema for saved portfolio
    # compatibility.
    if (meta.get("status") == "user_confirmed"
            and meta.get("value") == coupon_scenario_signature(bond)
            and meta.get("origin", "manual") in ("manual", "import")):
        return True
    return (meta.get("status") == "model" and bond.schedule_status == "model"
            and bool(str(meta.get("source", "")).strip())
            and bool(bond.schedule_source.strip())
            and meta.get("value") == _legacy_coupon_scenario_signature(bond))


def confirm_coupon_scenario(bond: Bond, *, source: str, confirmed: bool,
                            checked_on: date | None = None) -> Bond:
    """Return a confirmed scenario, recording only its internal creation path."""
    if source not in ("manual", "import"):
        raise ValueError("Внутренний путь сценария должен быть 'manual' или 'import'.")
    if not confirmed:
        raise ValueError("Подтвердите полноту сценария будущих выплат.")
    if not bond.cashflows:
        raise ValueError(SCENARIO_REQUIRED)
    result = copy.deepcopy(bond)
    result.schedule_kind, result.schedule_status = "scenario", "user"
    result.schedule_confirmed, result.schedule_source = True, ""
    result.provenance["coupon_scenario"] = {"value": coupon_scenario_signature(result),
        "status": "user_confirmed", "origin": source,
        "checked_on": (checked_on or date.today()).isoformat()}
    return result


def current_coupon_period(bond: Bond) -> tuple[dict | None, str]:
    """Validate the entered current period independently of future CF/YTM.

    The explicit rule allocates the entered coupon amount by convention days.
    """
    meta = bond.provenance.get("current_coupon_period", {})
    value = meta.get("value")
    try:
        if not isinstance(value, dict):
            return None, "Недостаточно данных: укажите границы текущего периода НКД."
        start, end = parse_date(value.get("start_date")), parse_date(value.get("end_date"))
        amount = value.get("coupon_amount")
        if not start or not end or end <= start or amount is None or isinstance(amount, bool):
            raise ValueError
        amount = float(amount)
        if not math.isfinite(amount) or amount < 0:
            raise ValueError
        if value.get("accrual_method") != "coupon_fraction":
            return None, "Правило НКД не задано или не реализовано."
        if not bond.valuation_date or not start <= bond.valuation_date <= end:
            return None, "Дата оценки вне введённого текущего купонного периода."
        return {**value, "start_date": start, "end_date": end, "coupon_amount": amount}, ""
    except (ValueError, TypeError, OverflowError, AttributeError):
        return None, "Некорректные параметры текущего купонного периода."
