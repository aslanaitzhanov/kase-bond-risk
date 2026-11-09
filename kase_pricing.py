"""KASE coupon-bond yield/dirty-price calculation (Methodology §§11-14, 18).

This module is deliberately separate from the calculator's default Actual/365F
pricing. It is selected per bond and currently requires the explicitly selected
KASE 30/360 basis. Amounts are returned in the same units as cashflows.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Iterable

from models import CashFlow
from day_count_conventions import KASE_30_360
from schedule import shift_months


KASE_PRICING_METHOD = "KASE coupon formula (30/360)"
ACT365F_PRICING_METHOD = "Actual/365 Fixed (standard)"


def kase_day_count_days(start: date, end: date) -> int:
    """KASE §18(1), with the stated conditional adjustment for day 31 only."""
    if end < start:
        return -kase_day_count_days(end, start)
    d1, d2 = start.day, end.day
    if d1 == 31:
        d1 = 30
    if d2 == 31 and d1 >= 30:
        d2 = 30
    return 360 * (end.year - start.year) + 30 * (end.month - start.month) + d2 - d1


def period_parameters(
    cashflows: Iterable[CashFlow], valuation_date: date, frequency: int,
) -> list[dict[str, object]]:
    """Return each future CF's m_i and F_i from the supplied regular coupon grid.

    m_i = T0/T_i with T0=360; F_i is remaining 30/360 days to payment / 360.
    If the input omits the previous coupon boundary, the previous scheduled date
    is derived from this cashflow date and frequency, not from an invented CF.
    """
    if isinstance(frequency, bool) or not isinstance(frequency, int) or frequency <= 0 or 12 % frequency:
        raise ValueError("Для методики KASE частота должна делить 12 месяцев.")
    all_flows = list(cashflows)
    future = sorted((flow for flow in all_flows if flow.payment_date > valuation_date), key=lambda f: f.payment_date)
    if not future:
        raise ValueError("Нет будущих денежных потоков для расчёта KASE.")
    coupon_dates = sorted({f.payment_date for f in all_flows if f.coupon_amount > 0})
    month_step = 12 // frequency
    params = []
    for flow in future:
        if not math.isfinite(flow.amount) or flow.amount <= 0:
            raise ValueError("Будущие денежные потоки KASE должны быть конечными и положительными.")
        earlier = [d for d in coupon_dates if d < flow.payment_date]
        start = max(earlier) if earlier else shift_months(flow.payment_date, -month_step)
        period_days = kase_day_count_days(start, flow.payment_date)
        remaining_days = kase_day_count_days(valuation_date, flow.payment_date)
        if period_days <= 0 or remaining_days <= 0:
            raise ValueError("Период KASE должен иметь положительную длину и лежать после даты оценки.")
        m_i = 360.0 / period_days
        f_i = remaining_days / 360.0
        params.append({"flow": flow, "period_start": start, "period_days_30_360": period_days,
                       "remaining_days_30_360": remaining_days, "m_i": m_i, "F_i": f_i})
    return params


def kase_price_metrics(
    cashflows: Iterable[CashFlow], valuation_date: date, ytm_pct: float, frequency: int,
) -> dict[str, object]:
    """KASE periodic dirty price and yield derivatives in cashflow currency units."""
    if not math.isfinite(ytm_pct):
        raise ValueError("YTM должна быть конечным числом.")
    y = ytm_pct / 100.0
    terms = period_parameters(cashflows, valuation_date, frequency)
    price = 0.0
    weighted_f = 0.0
    weighted_modified = 0.0
    weighted_convexity = 0.0
    rows = []
    for term in terms:
        flow = term["flow"]
        assert isinstance(flow, CashFlow)
        m_i, f_i = float(term["m_i"]), float(term["F_i"])
        base = 1.0 + y / m_i
        if base <= 0:
            raise ValueError("Недопустимая доходность: 1 + YTM/(100 m_i) должно быть больше нуля.")
        pv = flow.amount / base ** (m_i * f_i)
        if not math.isfinite(pv):
            raise ValueError("Приведённая стоимость вышла за числовой диапазон методики KASE.")
        price += pv
        weighted_f += f_i * pv
        weighted_modified += (f_i / base) * pv
        weighted_convexity += (f_i * (f_i + 1.0 / m_i) / (base * base)) * pv
        rows.append({**term, "cashflow_kzt": flow.amount, "discount_factor": 1.0 / base ** (m_i * f_i),
                     "present_value_kzt": pv})
    if price <= 0 or not math.isfinite(price):
        raise ValueError("Dirty price по методике KASE должна быть конечной и положительной.")
    macaulay = weighted_f / price
    modified = weighted_modified / price
    convexity = weighted_convexity / price
    return {"dirty_price": price, "macaulay_duration_years": macaulay,
            "modified_duration_years": modified, "convexity": convexity,
            "cashflow_terms": rows}


def kase_ytm_from_price(
    cashflows: Iterable[CashFlow], valuation_date: date, price: float, frequency: int, *,
    price_type: str = "dirty", accrued_interest_amount: float = 0.0,
) -> float:
    """Invert KASE-method dirty price; clean price receives accrued interest once."""
    if price_type not in ("clean", "dirty"):
        raise ValueError("Укажите clean или dirty price.")
    if not math.isfinite(price) or price <= 0:
        raise ValueError("Цена должна быть конечной и положительной.")
    if not math.isfinite(accrued_interest_amount) or accrued_interest_amount < 0:
        raise ValueError("НКД должен быть конечной неотрицательной суммой.")
    target = price + accrued_interest_amount if price_type == "clean" else price
    flows = list(cashflows)
    params = period_parameters(flows, valuation_date, frequency)
    lowest_m = min(float(item["m_i"]) for item in params)
    low, high = -100.0 * lowest_m + 1e-6, 100.0

    def model(ytm: float) -> float:
        return float(kase_price_metrics(flows, valuation_date, ytm, frequency)["dirty_price"])

    if target >= model(low):
        raise ValueError("Цена не соответствует доходности в допустимой области методики KASE.")
    while model(high) > target and high < 1_000_000:
        high *= 2
    if model(high) > target:
        raise ValueError("Не удалось заключить YTM в числовой диапазон методики KASE.")
    for _ in range(180):
        mid = (low + high) / 2
        if model(mid) > target:
            low = mid
        else:
            high = mid
    return (low + high) / 2

