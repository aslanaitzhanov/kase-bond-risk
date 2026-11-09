"""Bond pricing, accrued interest, duration, convexity and portfolio stress calculations.

Discounting uses periodic annual-compounded YTM: PV = CF / (1+y/m) ** (m*t),
where t is Actual/365 Fixed from valuation date to payment date. This is a model
convention, not an assertion about the convention of any particular KASE issue.
"""
from __future__ import annotations

import math
import calendar
from datetime import date
from typing import Iterable

import numpy as np

from models import Bond, CashFlow
from day_count_conventions import KASE_30_360
from schedule import shift_months
from validation import validate_bond
from coupon_scenarios import (current_coupon_period, requires_coupon_scenario,
                              SCENARIO_LABEL, SCENARIO_NOTE)
from kase_pricing import (ACT365F_PRICING_METHOD, KASE_PRICING_METHOD,
                          kase_day_count_days, kase_price_metrics, kase_ytm_from_price)


def _thirty_360_days(start: date, end: date, convention: str) -> int:
    d1, d2 = start.day, end.day
    if convention == KASE_30_360:
        # KASE methodology, paragraph 18(1), current revision effective 27.02.2026.
        # Deliberately no US/NASD end-of-February adjustment.
        if d1 == 31:
            d1 = 30
        if d2 == 31 and d1 >= 30:
            d2 = 30
    elif convention == "30E/360":
        d1, d2 = min(d1, 30), min(d2, 30)
    else:  # 30/360 US (NASD)
        if d1 == 31 or (start.month == 2 and d1 == calendar.monthrange(start.year, start.month)[1]):
            d1 = 30
        if d2 == 31 and d1 == 30:
            d2 = 30
        if start.month == 2 and start.day == calendar.monthrange(start.year, start.month)[1] and end.month == 2 and end.day == calendar.monthrange(end.year, end.month)[1]:
            d1, d2 = 30, 30
    return 360 * (end.year - start.year) + 30 * (end.month - start.month) + d2 - d1


def day_count_days(start: date, end: date, convention: str) -> int:
    """Return convention day count over [start, end]."""
    if end < start:
        return -day_count_days(end, start, convention)
    if convention in ("30/360 US", "30E/360", KASE_30_360):
        return _thirty_360_days(start, end, convention)
    return (end - start).days


def year_fraction(start: date, end: date, convention: str = "Actual/365 Fixed", *, period_start: date | None = None, period_end: date | None = None, frequency: int | None = None) -> float:
    """Return year fraction for supported accrual conventions.

    Actual/Actual uses the supplied coupon period's actual-day fraction scaled by
    coupon frequency. Other conventions use their standard 365/360 denominator.
    """
    if end < start:
        return -year_fraction(end, start, convention, period_start=period_start, period_end=period_end, frequency=frequency)
    if convention == "Actual/365 Fixed":
        return (end - start).days / 365.0
    if convention in ("30/360 US", "30E/360", KASE_30_360):
        return day_count_days(start, end, convention) / 360.0
    if convention == "Actual/Actual (купонный период)":
        if period_start is None or period_end is None or frequency is None or frequency <= 0 or period_end <= period_start:
            raise ValueError("Для Actual/Actual нужны границы купонного периода и положительная частота выплат.")
        return ((end - start).days / (period_end - period_start).days) / frequency
    raise ValueError(f"Неподдерживаемая конвенция: {convention}")


def accrued_interest(
    valuation_date: date,
    previous_coupon_date: date,
    next_coupon_date: date,
    face_value: float,
    *,
    convention: str,
    coupon_rate_pct: float | None = None,
    coupon_amount: float | None = None,
    frequency: int | None = None,
) -> float:
    """Calculate accrued coupon per bond under an explicitly selected convention.

    The explicit coupon-ratio convention allocates the scheduled cash coupon by actual elapsed days
    within the actual coupon period. For Act/365F and 30/360, a provided annual
    rate is accrued on face value; if only a cash coupon is known, its fraction of
    convention days in the period is used and that assumption must be disclosed.
    """
    if convention not in ("Actual/365 Fixed", "Actual/Actual (купонный период)", "30/360 US", "30E/360", KASE_30_360):
        raise ValueError("Неизвестная конвенция НКД.")
    if coupon_rate_pct is not None and (not math.isfinite(coupon_rate_pct) or coupon_rate_pct < 0):
        raise ValueError("Ставка купона должна быть конечной и неотрицательной.")
    if previous_coupon_date > valuation_date or valuation_date > next_coupon_date or next_coupon_date <= previous_coupon_date:
        raise ValueError("Дата оценки должна находиться между последовательными купонными датами.")
    if valuation_date == previous_coupon_date or valuation_date == next_coupon_date:
        return 0.0
    if not math.isfinite(face_value) or face_value <= 0:
        raise ValueError("Номинал должен быть положительным и конечным.")
    elapsed_actual = (valuation_date - previous_coupon_date).days
    period_actual = (next_coupon_date - previous_coupon_date).days
    if convention == "Actual/Actual (купонный период)":
        if coupon_amount is None:
            if coupon_rate_pct is None or not frequency or frequency <= 0:
                raise ValueError("Для Actual/Actual нужны сумма купона либо ставка и частота.")
            coupon_amount = face_value * coupon_rate_pct / 100.0 / frequency
        if not math.isfinite(coupon_amount) or coupon_amount < 0:
            raise ValueError("Сумма купона должна быть конечной и неотрицательной.")
        return float(coupon_amount) * elapsed_actual / period_actual
    elapsed = day_count_days(previous_coupon_date, valuation_date, convention)
    total = day_count_days(previous_coupon_date, next_coupon_date, convention)
    if total <= 0:
        raise ValueError("Длина купонного периода по выбранной конвенции должна быть положительной.")
    if coupon_rate_pct is not None:
        denominator = 365.0 if convention == "Actual/365 Fixed" else 360.0
        return face_value * coupon_rate_pct / 100.0 * elapsed / denominator
    if coupon_amount is None or not math.isfinite(coupon_amount) or coupon_amount < 0:
        raise ValueError("Для расчёта НКД нужна ставка купона или сумма купона.")
    return float(coupon_amount) * elapsed / total


def _future_flows(cashflows: Iterable[CashFlow], valuation_date: date) -> list[CashFlow]:
    return sorted((flow for flow in cashflows if flow.payment_date > valuation_date), key=lambda flow: flow.payment_date)


def dirty_price_from_cashflows(
    cashflows: Iterable[CashFlow], valuation_date: date, ytm_pct: float, frequency: int, *, reject_invalid: bool = True,
) -> float:
    """Discount explicit future cash flows with periodic annual compounding."""
    if not isinstance(frequency, int) or frequency <= 0:
        raise ValueError("Частота начисления m должна быть положительным целым числом.")
    if not math.isfinite(ytm_pct):
        raise ValueError("YTM должна быть конечным числом.")
    y = ytm_pct / 100.0
    base = 1.0 + y / frequency
    if base <= 0:
        raise ValueError("Недопустимая доходность: 1 + YTM / m должно быть больше нуля.")
    flows = _future_flows(cashflows, valuation_date)
    if not flows:
        raise ValueError("Нет будущих денежных потоков для дисконтирования.")
    values = []
    for flow in flows:
        if not math.isfinite(flow.amount) or flow.amount < 0:
            raise ValueError("Денежные потоки должны быть конечными и неотрицательными.")
        t = (flow.payment_date - valuation_date).days / 365.0
        try:
            pv = flow.amount / base ** (frequency * t)
        except (OverflowError, ZeroDivisionError) as exc:
            raise ValueError("Приведённая стоимость вышла за числовой диапазон модели.") from exc
        if not math.isfinite(pv):
            raise ValueError("Получена неконечная приведённая стоимость.")
        values.append(pv)
    price = float(np.sum(values))
    if reject_invalid and (not math.isfinite(price) or price <= 0):
        raise ValueError("Теоретическая dirty price должна быть конечной и положительной.")
    return price


def ytm_from_price(cashflows: Iterable[CashFlow], valuation_date: date, price: float,
                   frequency: int, *, price_type: str = "dirty",
                   accrued_interest_amount: float = 0.0,
                   pricing_method: str = ACT365F_PRICING_METHOD,
                   day_count: str = "Actual/365 Fixed") -> float:
    """Invert this model's periodic-compounding price for a complete cashflow set.

    A clean price is converted to dirty once by adding accrued interest. A dirty
    price is used as entered and does not include the supplied accrued amount.
    """
    if price_type not in ("clean", "dirty"):
        raise ValueError("Укажите clean или dirty price.")
    if not math.isfinite(price) or price <= 0:
        raise ValueError("Цена должна быть конечной и положительной.")
    if not math.isfinite(accrued_interest_amount) or accrued_interest_amount < 0:
        raise ValueError("НКД должен быть конечной неотрицательной суммой.")
    if not isinstance(frequency, int) or isinstance(frequency, bool) or frequency <= 0:
        raise ValueError("Частота начисления m должна быть положительным целым числом.")
    if pricing_method == KASE_PRICING_METHOD:
        if day_count != KASE_30_360:
            raise ValueError("Методика KASE требует явно выбранную конвенцию 30/360 KASE.")
        return kase_ytm_from_price(cashflows, valuation_date, price, frequency,
                                  price_type=price_type,
                                  accrued_interest_amount=accrued_interest_amount)
    if pricing_method != ACT365F_PRICING_METHOD:
        raise ValueError("Неизвестная методика дисконтирования.")
    target = price + accrued_interest_amount if price_type == "clean" else price
    future = _future_flows(cashflows, valuation_date)
    if not future:
        raise ValueError("Для расчёта YTM нужен полный график будущих денежных потоков.")

    def model_price(yield_pct: float) -> float:
        try:
            return dirty_price_from_cashflows(future, valuation_date, yield_pct, frequency)
        except (OverflowError, ValueError):
            return math.inf

    lower = -100.0 * frequency + 1e-6
    lower_price = model_price(lower)
    if target >= lower_price:
        raise ValueError("Цена не соответствует доходности в допустимой области модели.")
    upper = 100.0
    upper_price = model_price(upper)
    while upper_price > target and upper < 1_000_000:
        upper *= 2
        upper_price = model_price(upper)
    if upper_price > target:
        raise ValueError("Не удалось заключить YTM в числовой диапазон модели.")
    for _ in range(160):
        middle = (lower + upper) / 2
        if model_price(middle) > target:
            lower = middle
        else:
            upper = middle
    result = (lower + upper) / 2
    if not math.isfinite(result):
        raise ValueError("Численный расчёт YTM не сошёлся.")
    return result


def accrued_for_bond(bond: Bond) -> tuple[float | None, str]:
    """Find adjacent coupon dates, subject to issue-specific accrual support."""
    if bond.valuation_date is None:
        return None, "Не задана дата оценки."
    if bond.maturity_date and bond.valuation_date >= bond.maturity_date:
        return None, "На дату погашения и после неё расчёт позиции недоступен."
    if requires_coupon_scenario(bond) or "current_coupon_period" in bond.provenance:
        period, error = current_coupon_period(bond)
        if period is None:
            return None, error
        try:
            convention = period.get("day_count")
            if convention not in ("Actual/365 Fixed", "Actual/Actual (купонный период)", "30/360 US", "30E/360", KASE_30_360):
                raise ValueError("Неизвестная конвенция текущего периода.")
            start, end = period["start_date"], period["end_date"]
            total = day_count_days(start, end, convention)
            if total <= 0:
                raise ValueError("Длина периода по конвенции должна быть положительной.")
            # Same explicit coupon allocation as accrued_interest's amount branch.
            # Never use a last published annual rate or a future scenario for AI.
            accrued = period["coupon_amount"] * day_count_days(start, bond.valuation_date, convention) / total
            return accrued, (f"Введённый текущий период {start:%d.%m.%Y} — {end:%d.%m.%Y}; "
                f"{convention}. НКД — доля суммы купона по дням конвенции. "
                "Дата окончания начисления не означает дату перевода выплаты.")
        except (ValueError, TypeError, ArithmeticError) as exc:
            return None, str(exc)
    if bond.face_value is None or not math.isfinite(bond.face_value) or bond.face_value <= 0:
        return None, "Для НКД нужен конечный положительный номинал."
    if bond.maturity_date and bond.valuation_date >= bond.maturity_date:
        return None, "На дату погашения и после неё расчёт позиции недоступен."
    if bond.coupon_rate_pct == 0:
        return 0.0, "Бескупонная облигация: НКД равен нулю."
    if bond.nonstandard_coupon_period and not bond.stub_accrual_confirmed:
        return None, "Для НКД в нестандартном периоде задайте правило начисления."
    coupon_flows = sorted((x for x in bond.cashflows if x.coupon_amount > 0), key=lambda x: x.payment_date)
    previous = [x for x in coupon_flows if x.payment_date <= bond.valuation_date]
    following = [x for x in coupon_flows if x.payment_date >= bond.valuation_date]
    if not previous and following and bond.issue_date and bond.issue_date <= bond.valuation_date:
        previous = [CashFlow(bond.issue_date)]
    if not previous or not following:
        return None, "В графике не заданы обе границы текущего купонного периода."
    prev_flow, next_flow = previous[-1], following[0]
    if prev_flow.payment_date == next_flow.payment_date:
        return 0.0, "Дата оценки совпадает с купонной выплатой."
    if (not bond.nonstandard_coupon_period and bond.coupon_frequency in (1, 2, 3, 4, 6, 12)
            and shift_months(next_flow.payment_date, -12 // bond.coupon_frequency) != prev_flow.payment_date):
        return None, "Границы периода не соответствуют частоте; уточните предыдущий купон или нестандартное правило НКД."
    try:
        accrued = accrued_interest(
            bond.valuation_date, prev_flow.payment_date, next_flow.payment_date, bond.face_value,
            convention=bond.day_count, coupon_rate_pct=bond.coupon_rate_pct,
            coupon_amount=next_flow.coupon_amount, frequency=bond.coupon_frequency,
        )
        if accrued < 0 or accrued > next_flow.coupon_amount + max(0.01, next_flow.coupon_amount * 1e-8):
            return None, "НКД выходит за границы суммы следующего купона. Проверьте конвенцию, ставку и сумму выплаты."
        confirmation = " Правило НКД для нестандартного периода отмечено пользователем." if bond.nonstandard_coupon_period else ""
        return accrued, f"Период {prev_flow.payment_date:%d.%m.%Y} — {next_flow.payment_date:%d.%m.%Y}; {bond.day_count}.{confirmation}"
    except ValueError as exc:
        return None, str(exc)


def _price_with_metrics(bond: Bond, ytm_pct: float) -> tuple[float, float, float, float]:
    """Return dirty price, Macaulay duration, modified duration and convexity."""
    assert bond.valuation_date is not None and bond.coupon_frequency is not None
    if bond.pricing_method == KASE_PRICING_METHOD:
        if bond.day_count != KASE_30_360:
            raise ValueError("Методика KASE требует конвенцию 30/360 KASE.")
        result = kase_price_metrics(bond.cashflows, bond.valuation_date, ytm_pct, bond.coupon_frequency)
        return (float(result["dirty_price"]), float(result["macaulay_duration_years"]),
                float(result["modified_duration_years"]), float(result["convexity"]))
    if bond.pricing_method != ACT365F_PRICING_METHOD:
        raise ValueError("Неизвестная методика дисконтирования.")
    price = dirty_price_from_cashflows(bond.cashflows, bond.valuation_date, ytm_pct, bond.coupon_frequency)
    y, m = ytm_pct / 100.0, bond.coupon_frequency
    denominator = 1 + y / m
    flows = _future_flows(bond.cashflows, bond.valuation_date)
    mac_terms: list[float] = []
    convex_terms: list[float] = []
    for flow in flows:
        t = (flow.payment_date - bond.valuation_date).days / 365.0
        pv = flow.amount / denominator ** (m * t)
        mac_terms.append(t * pv)
        convex_terms.append(flow.amount * t * (t + 1 / m) / denominator ** (m * t + 2))
    macaulay = float(np.sum(mac_terms) / price)
    modified = macaulay / denominator
    convexity = float(np.sum(convex_terms) / price)
    return price, macaulay, modified, convexity


def numeric_sensitivity(bond: Bond, step: float = 1e-4) -> dict[str, float]:
    """Check analytical price sensitivity by centered finite differences (step in decimal yield)."""
    if requires_coupon_scenario(bond):
        report = validate_bond(bond, require_calculable=True, mode="model")
        if report.errors:
            raise ValueError("; ".join(report.errors))
    if bond.ytm_pct is None or bond.valuation_date is None or bond.coupon_frequency is None:
        raise ValueError("Для численной проверки необходимы YTM, дата оценки и частота.")
    if step <= 0 or not math.isfinite(step):
        raise ValueError("Шаг конечных разностей должен быть положительным.")
    y = bond.ytm_pct / 100
    periodic_base = 1.0 + y / bond.coupon_frequency
    effective_step = min(step, periodic_base * bond.coupon_frequency / 2)
    if effective_step <= 0 or not math.isfinite(effective_step):
        raise ValueError("Невозможно подобрать шаг производных в области модели.")
    p0, pm, pp = (
        dirty_price_for_bond(bond, (y + delta) * 100)
        for delta in (0.0, -effective_step, effective_step)
    )
    first = (pp - pm) / (2 * effective_step)
    second = (pp - 2 * p0 + pm) / (effective_step * effective_step)
    return {
        "numerical_first_derivative": first,
        "numerical_second_derivative": second,
        "numerical_modified_duration": -first / p0,
        "numerical_convexity": second / p0,
    }


def bond_analytics(bond: Bond, mode: str | None = None) -> dict[str, object]:
    """Calculate a bond only when its schedule and model inputs pass validation."""
    report = validate_bond(bond, require_calculable=True, mode=mode)
    if report.errors:
        raise ValueError("; ".join(report.errors))
    assert bond.valuation_date and bond.maturity_date and bond.ytm_pct is not None
    assert bond.coupon_frequency is not None
    dirty, macaulay, modified, convexity = _price_with_metrics(bond, bond.ytm_pct)
    accrued, accrued_note = accrued_for_bond(bond)
    clean = dirty - accrued if accrued is not None else None
    if clean is not None and clean <= 0:
        raise ValueError("Clean price получилась неположительной; проверьте график и НКД.")
    numerical = numeric_sensitivity(bond)
    position_value = dirty * bond.quantity
    return {
        "valuation_kind": "coupon_scenario" if requires_coupon_scenario(bond) else "explicit_cashflows",
        "valuation_label": SCENARIO_LABEL if requires_coupon_scenario(bond) else "Расчёт по заданным денежным потокам",
        "valuation_note": SCENARIO_NOTE if requires_coupon_scenario(bond) else "",
        "dirty_price": dirty,
        "clean_price": clean,
        "accrued_per_bond": accrued,
        "accrued_position": accrued * bond.quantity if accrued is not None else None,
        "accrued_note": accrued_note,
        "macaulay_duration_years": macaulay,
        "macaulay_duration_days": macaulay * (360 if bond.pricing_method == KASE_PRICING_METHOD else 365),
        "modified_duration_years": modified,
        "convexity": convexity,
        "position_market_value": position_value,
        "numerical": numerical,
        "duration_abs_diff": abs(modified - numerical["numerical_modified_duration"]),
        "convexity_abs_diff": abs(convexity - numerical["numerical_convexity"]),
    }


def dirty_price_for_bond(bond: Bond, ytm_pct: float) -> float:
    """Price one bond with its explicitly selected method, preserving old defaults."""
    if bond.valuation_date is None or bond.coupon_frequency is None:
        raise ValueError("Для расчёта нужны дата оценки и частота купона.")
    if bond.pricing_method == KASE_PRICING_METHOD:
        if bond.day_count != KASE_30_360:
            raise ValueError("Методика KASE требует конвенцию 30/360 KASE.")
        return float(kase_price_metrics(bond.cashflows, bond.valuation_date, ytm_pct,
                                        bond.coupon_frequency)["dirty_price"])
    if bond.pricing_method != ACT365F_PRICING_METHOD:
        raise ValueError("Неизвестная методика дисконтирования.")
    return dirty_price_from_cashflows(bond.cashflows, bond.valuation_date, ytm_pct, bond.coupon_frequency)


def portfolio_weights(bonds: list[Bond], mode: str, analytics: dict[str, dict[str, object]]) -> dict[str, float]:
    """Create normalized weights for the selected included bonds."""
    included = [b for b in bonds if b.included]
    if not included:
        return {}
    if mode == "Равные веса":
        raw = {b.position_key: 1.0 for b in included}
    elif mode == "По номинальной стоимости":
        raw = {b.position_key: b.face_value * b.quantity for b in included}
    elif mode == "По рыночной стоимости":
        if any(b.position_key not in analytics for b in included):
            raise ValueError("Рыночные веса недоступны: хотя бы одна включённая облигация не рассчитана.")
        raw = {b.position_key: float(analytics[b.position_key]["position_market_value"]) for b in included}
    else:
        raise ValueError("Неизвестный режим весов портфеля.")
    total = sum(raw.values())
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Сумма весов должна быть конечной и положительной.")
    return {isin: value / total for isin, value in raw.items()}


def portfolio_analytics(bonds: list[Bond], mode: str, calculation_mode: str | None = None) -> dict[str, object]:
    """Aggregate only a complete included portfolio; incomplete positions block totals."""
    included = [b for b in bonds if b.included]
    if not included:
        return {"complete": False, "errors": ["Нет включённых облигаций."], "analytics": {}, "weights": {}}
    if len({b.position_key for b in included}) != len(included):
        return {"complete": False, "errors": ["Включённые позиции содержат повторяющийся идентификатор записи."], "analytics": {}, "weights": {}}
    computed: dict[str, dict[str, object]] = {}
    errors: list[str] = []
    if len({b.valuation_date for b in included}) > 1:
        errors.append("У включённых позиций разные даты оценки.")
    if len({b.currency for b in included}) > 1:
        errors.append("Агрегация разных валют требует валютных курсов и не поддерживается.")
    for bond in included:
        try:
            computed[bond.position_key] = bond_analytics(bond, calculation_mode)  # type: ignore[assignment]
        except (ValueError, ArithmeticError) as exc:
            errors.append(f"{bond.name} (позиция {bond.record_id[:8]}): {exc}")
    if errors:
        return {"complete": False, "errors": errors, "analytics": computed, "weights": {}}
    weights = portfolio_weights(included, mode, computed)
    market_value = sum(float(computed[b.position_key]["position_market_value"]) for b in included)
    risk = {
        key: sum(weights[b.position_key] * float(computed[b.position_key][field]) for b in included)
        for key, field in (
            ("macaulay_duration_years", "macaulay_duration_years"),
            ("modified_duration_years", "modified_duration_years"),
            ("convexity", "convexity"),
        )
    }
    market_weights = {b.position_key: float(computed[b.position_key]["position_market_value"]) / market_value for b in included}
    risk_market_value = {
        key: sum(market_weights[b.position_key] * float(computed[b.position_key][field]) for b in included)
        for key, field in (
            ("macaulay_duration_years", "macaulay_duration_years"),
            ("modified_duration_years", "modified_duration_years"),
            ("convexity", "convexity"),
        )
    }
    weighted_ytm = sum(market_weights[b.position_key] * float(b.ytm_pct) for b in included if b.ytm_pct is not None)
    return {
        "complete": True, "errors": [], "analytics": computed, "weights": weights,
        "contains_coupon_scenarios": any(requires_coupon_scenario(b) for b in included),
        "valuation_note": SCENARIO_NOTE if any(requires_coupon_scenario(b) for b in included) else "",
        "market_value": market_value, "risk": risk, "market_value_risk": risk_market_value,
        "market_weights": market_weights, "weighted_ytm_pct": weighted_ytm,
    }


def stress_scenarios(bonds: list[Bond], shocks_bp: Iterable[int], *, per_bond_shocks: dict[str, float] | None = None, calculation_mode: str | None = None,
                     weight_mode: str = "По рыночной стоимости") -> list[dict[str, object]]:
    """Return selected-weight analytical returns and actual position cash P&L.

    Analytical weights never rescale the currency result of actual quantities.
    Every instrument retains its own pricing method and cashflow scenario.
    """
    included = [b for b in bonds if b.included]
    portfolio = portfolio_analytics(bonds, weight_mode, calculation_mode)
    if not portfolio["complete"]:
        raise ValueError("Стресс-тест недоступен: " + "; ".join(portfolio["errors"]))  # type: ignore[arg-type]
    base_analytics: dict[str, dict[str, object]] = portfolio["analytics"]  # type: ignore[assignment]
    rows: list[dict[str, object]] = []
    for shock in shocks_bp:
        if not math.isfinite(float(shock)):
            raise ValueError("Сдвиг должен быть конечным числом.")
        linear_pnl = convex_pnl = exact_value = 0.0
        analytical_linear = analytical_convex = analytical_exact = 0.0
        exact_changes: dict[str, float] = {}
        position_results = []
        valid = True
        reason = ""
        for bond in included:
            assert bond.valuation_date and bond.coupon_frequency and bond.ytm_pct is not None
            bp = per_bond_shocks.get(bond.position_key, float(shock)) if per_bond_shocks else float(shock)
            if not math.isfinite(bp):
                raise ValueError("Индивидуальный сдвиг должен быть конечным числом.")
            delta_y = bp / 10000.0
            new_yield_pct = bond.ytm_pct + bp / 100.0
            base = base_analytics[bond.position_key]
            position = float(base["position_market_value"])
            mod = float(base["modified_duration_years"])
            conv = float(base["convexity"])
            linear_return = -mod * delta_y
            convex_return = linear_return + 0.5 * conv * delta_y * delta_y
            linear_pnl += position * linear_return
            convex_pnl += position * convex_return
            weight = portfolio["weights"][bond.position_key]
            analytical_linear += weight * linear_return
            analytical_convex += weight * convex_return
            if 1 + new_yield_pct / 100 / bond.coupon_frequency <= 0:
                valid = False
                reason = f"{bond.name}: доходность после сдвига выходит из области модели."
                continue
            try:
                new_price = dirty_price_for_bond(bond, new_yield_pct)
            except ValueError as exc:
                valid, reason = False, f"{bond.name}: {exc}"
                continue
            new_position = new_price * bond.quantity
            analytical_exact += weight * (new_price / float(base["dirty_price"]) - 1)
            exact_value += new_position
            exact_changes[bond.position_key] = new_position - position
            position_results.append({"name": bond.name, "is_model": bond.is_model,
                "record_id": bond.record_id, "position_key": bond.position_key, "shock_bp": bp,
                "old_ytm_pct": bond.ytm_pct, "new_ytm_pct": new_yield_pct,
                "old_dirty_price": base["dirty_price"], "new_dirty_price": new_price,
                "base_value_kzt": position, "new_value_kzt": new_position,
                "linear_pnl_kzt": position * linear_return,
                "convexity_contribution_kzt": position * (convex_return - linear_return),
                "approx_pnl_kzt": position * convex_return, "exact_pnl_kzt": new_position - position,
                "approx_error_kzt": position * convex_return - (new_position - position)})
        base_value = float(portfolio["market_value"])
        rows.append({
            "valuation_note": portfolio.get("valuation_note", ""),
            "shock_bp": shock,
            "weight_mode": weight_mode,
            "analytical_weights": portfolio["weights"],
            "analytical_linear_change_pct": analytical_linear * 100,
            "analytical_convexity_change_pct": analytical_convex * 100,
            "analytical_exact_change_pct": analytical_exact * 100 if valid else None,
            "linear_change_pct": linear_pnl / base_value * 100,
            "convexity_change_pct": convex_pnl / base_value * 100,
            "linear_change_kzt": linear_pnl,
            "convexity_change_kzt": convex_pnl,
            "convexity_contribution_kzt": convex_pnl - linear_pnl,
            "approx_error_kzt": convex_pnl - (exact_value - base_value) if valid else None,
            "convexity_value_kzt": base_value + convex_pnl,
            "exact_change_kzt": exact_value - base_value if valid else None,
            "exact_value_kzt": exact_value if valid else None,
            "exact_change_pct": (exact_value / base_value - 1) * 100 if valid else None,
            "valid": valid,
            "reason": reason,
            "position_changes": exact_changes if valid else {},
            "positions": position_results if valid else [],
            "scenario_type": "Индивидуальный" if per_bond_shocks else "Параллельный сдвиг YTM",
        })
    return rows
