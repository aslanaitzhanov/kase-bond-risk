"""Calendar schedules. No business-day adjustment or unknown stub coupon is inferred."""
from __future__ import annotations

import calendar
import math
from datetime import date
from typing import Iterable

from models import Bond, CashFlow, parse_date


def shift_months(anchor: date, months: int) -> date:
    """Shift from the original anchor, preserving end-of-month roll without drift."""
    index = anchor.year * 12 + anchor.month - 1 + months
    year, month0 = divmod(index, 12)
    last = calendar.monthrange(year, month0 + 1)[1]
    eom = anchor.day == calendar.monthrange(anchor.year, anchor.month)[1]
    return date(year, month0 + 1, last if eom else min(anchor.day, last))


def regular_dates(anchor: date, start: date, end: date, frequency: int) -> list[date]:
    if isinstance(frequency, bool) or not isinstance(frequency, int) or frequency not in (1, 2, 3, 4, 6, 12):
        raise ValueError("Для календарного графика частота должна делить 12 (1, 2, 3, 4, 6, 12).")
    if end < start:
        raise ValueError("Конец графика раньше начала.")
    if (end - start).days > 365 * 150:
        raise ValueError("Горизонт генерации ограничен 150 годами.")
    step = 12 // frequency
    lower = ((start.year - anchor.year) * 12 + start.month - anchor.month) // step - 2
    upper = ((end.year - anchor.year) * 12 + end.month - anchor.month) // step + 2
    return [d for k in range(lower, upper + 1) if start <= (d := shift_months(anchor, k * step)) <= end]


def is_regular_coupon_date(anchor: date, candidate: date, frequency: int) -> bool:
    """Whether candidate falls on the anchor-based calendar-month grid."""
    if isinstance(frequency, bool) or not isinstance(frequency, int) or frequency not in (1, 2, 3, 4, 6, 12):
        raise ValueError("Для календарного графика частота должна делить 12.")
    step = 12 // frequency
    months = (candidate.year - anchor.year) * 12 + candidate.month - anchor.month
    return months % step == 0 and shift_months(anchor, months) == candidate


def validate_coupon_anchors(anchors: Iterable[date], frequency: int, maturity_date: date) -> list[date]:
    """Check every known coupon date against one stable calendar-month grid.

    A single dated coupon is enough to define a *preliminary* grid. Additional
    dates are consistency checks and need not equal the number of payments per
    year.
    """
    if isinstance(frequency, bool) or not isinstance(frequency, int) or frequency not in (1, 2, 4, 12):
        raise ValueError("Для регулярного графика выберите 1, 2, 4 или 12 выплат в год.")
    values = list(anchors)
    if any(not isinstance(value, date) for value in values):
        raise ValueError("Купонные даты должны быть календарными датами.")
    dates = sorted(set(values))
    if not dates:
        raise ValueError("Введите хотя бы одну известную купонную дату.")
    if any(value > maturity_date for value in dates):
        raise ValueError("Известная купонная дата позже погашения.")
    anchor = dates[0]
    mismatched = [value.isoformat() for value in dates[1:] if not is_regular_coupon_date(anchor, value, frequency)]
    if mismatched:
        raise ValueError("Опорные даты не соответствуют выбранной периодичности: " + ", ".join(mismatched))
    return dates


def generate_regular_schedule(bond: Bond, anchor_dates: Iterable[date], *, source: str,
                              status: str = "model", stub_amounts: dict[date, float] | None = None) -> list[CashFlow]:
    """Generate a fixed-coupon month-grid after validating all supplied anchors.

    The maturity date is never moved. If it misses the normal grid, the
    existing generator requires an explicit contractual final-stub amount.
    """
    if not bond.maturity_date:
        raise ValueError("Укажите дату погашения.")
    if bond.coupon_type == "fixed" and bond.coupon_rate_pct == 0:
        # A zero-coupon bond has a redemption date, not fictitious coupon
        # anchors. m still explicitly defines yield compounding.
        return generate_schedule(bond, bond.maturity_date, source=source, status=status)
    anchors = validate_coupon_anchors(anchor_dates, bond.coupon_frequency, bond.maturity_date)
    anchor = anchors[0]
    grid = regular_dates(anchor, bond.valuation_date, bond.maturity_date, bond.coupon_frequency)
    missing = [value.isoformat() for value in anchors if value not in grid and value < bond.valuation_date]
    if missing:
        # Historical anchors are allowed; test them directly against the same
        # anchor grid rather than requiring them to be in the future range.
        missing = [value.isoformat() for value in anchors if not is_regular_coupon_date(anchor, value, bond.coupon_frequency)]
    if missing:
        raise ValueError("Опорные даты не попали в календарную сетку: " + ", ".join(missing))
    return generate_schedule(bond, anchor, source=source, status=status, stub_amounts=stub_amounts)


def generate_schedule(bond: Bond, anchor: date, *, source: str, status: str = "model",
                      stub_amounts: dict[date, float] | None = None) -> list[CashFlow]:
    """Generate regular coupons, keeping a previous coupon for accrued interest.

    Stub amounts must be supplied explicitly from terms (or an explicit model
    assumption). Both short and long first periods and short last periods are
    supported through first_coupon_date/issue_date and stub_amounts.
    """
    if bond.coupon_type != "fixed":
        raise ValueError("Для плавающих и неизвестных купонов генерация из одной ставки запрещена. Необходим сценарий будущих купонов.")
    if not bond.valuation_date or not bond.maturity_date or bond.maturity_date <= bond.valuation_date:
        raise ValueError("Задайте дату оценки и более позднюю дату погашения.")
    if bond.face_value is None or not math.isfinite(bond.face_value) or bond.face_value <= 0:
        raise ValueError("Задайте положительный номинал.")
    if bond.coupon_rate_pct is None or not math.isfinite(bond.coupon_rate_pct) or bond.coupon_rate_pct < 0:
        raise ValueError("Задайте конечную неотрицательную ставку купона.")
    if status not in ("model", "user", "verified"):
        raise ValueError("Некорректный технический статус графика.")
    m = bond.coupon_frequency
    if bond.coupon_rate_pct == 0:
        if isinstance(m, bool) or not isinstance(m, int) or m <= 0:
            raise ValueError("Для капитализации доходности нужна положительная целая частота m.")
        if bond.issue_date and not bond.issue_date <= bond.valuation_date:
            raise ValueError("Дата оценки предшествует началу начисления.")
        return [CashFlow(bond.maturity_date, 0.0, bond.face_value, status, source.strip())]
    # Validate frequency before integer calendar arithmetic.
    regular_dates(anchor, bond.valuation_date, bond.maturity_date, m)
    if anchor > bond.maturity_date:
        raise ValueError("Опорная купонная дата позже погашения.")
    if bond.issue_date and bond.issue_date >= bond.maturity_date:
        raise ValueError("Начало начисления должно предшествовать погашению.")
    if bond.first_coupon_date and (not bond.issue_date or bond.first_coupon_date <= bond.issue_date or bond.first_coupon_date > bond.maturity_date):
        raise ValueError("Для первого купона нужны согласованные даты начала начисления и выплаты.")
    step = 12 // m
    start = bond.issue_date or shift_months(bond.valuation_date, -step * 2)
    dates = regular_dates(anchor, start, bond.maturity_date, m)
    if bond.issue_date:
        dates = [d for d in dates if d > bond.issue_date]
    if bond.first_coupon_date:
        dates = [d for d in dates if d >= bond.first_coupon_date]
        if bond.first_coupon_date not in dates:
            raise ValueError("Первый купон должен совпадать с опорной сеткой; задайте его как опорную дату.")
    irregular: set[date] = set()
    if bond.issue_date and dates:
        if shift_months(anchor, ((dates[0].year - anchor.year) * 12 + dates[0].month - anchor.month) - step) != bond.issue_date:
            irregular.add(dates[0])
    if bond.maturity_date not in dates:
        dates.append(bond.maturity_date)
        irregular.add(bond.maturity_date)
    stub_amounts = stub_amounts or {}
    if irregular - stub_amounts.keys():
        missing = ", ".join(d.isoformat() for d in sorted(irregular - stub_amounts.keys()))
        raise ValueError(f"Нестандартный купон: задайте явную сумму по условиям выпуска на {missing}. Равный купон не подставляется.")
    if set(stub_amounts) - irregular:
        raise ValueError("Суммы нестандартных купонов заданы для дат без нестандартного периода.")
    regular_coupon = bond.face_value * bond.coupon_rate_pct / 100 / m
    previous = [d for d in dates if d <= bond.valuation_date]
    keep_from = previous[-1] if previous else bond.valuation_date
    flows = []
    for d in dates:
        if d < keep_from:
            continue
        amount = stub_amounts.get(d, regular_coupon)
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("Сумма купона должна быть конечной и неотрицательной.")
        redemption = bond.face_value if d == bond.maturity_date else 0.0
        if amount or redemption:
            flows.append(CashFlow(d, amount, redemption, status, source.strip()))
    return flows


def schedule_errors(bond: Bond) -> list[str]:
    """Independently check coupon coverage; a confirmation checkbox is no substitute."""
    errors = []
    if bond.coupon_type != "fixed":
        return errors  # Basic dates/amounts are checked in validation.py, without fixed-coupon assumptions.
    if not bond.cashflows or not bond.valuation_date or not bond.maturity_date or bond.maturity_date <= bond.valuation_date:
        return errors
    if bond.coupon_rate_pct is None or not math.isfinite(bond.coupon_rate_pct) or bond.coupon_rate_pct <= 0 or not bond.coupon_frequency:
        return errors
    coupons = [f for f in bond.cashflows if f.coupon_amount > 0]
    if not coupons:
        return ["В графике отсутствуют купонные выплаты."]
    if any(d > bond.valuation_date and d <= bond.maturity_date and d not in {f.payment_date for f in coupons} for d in bond.user_supplied_coupon_dates):
        errors.append("В графике отсутствует одна из введённых купонных дат.")
    if bond.nonstandard_coupon_period:
        known = {d for d in bond.user_supplied_coupon_dates if bond.valuation_date < d <= bond.maturity_date}
        actual = {f.payment_date for f in bond.cashflows if f.payment_date > bond.valuation_date}
        if not known:
            return errors + ["Для нестандартного графика укажите даты всех будущих выплат."]
        if known != actual:
            return errors + ["Неполный нестандартный график: даты денежных потоков должны совпадать с явно заданными датами выплат."]
        return errors
    else:
        anchor = coupons[-1].payment_date
        if bond.coupon_frequency in (1, 2, 3, 4, 6, 12):
            start = max(bond.valuation_date, bond.issue_date or bond.valuation_date)
            try:
                expected = {d for d in regular_dates(anchor, start, bond.maturity_date, bond.coupon_frequency) if d > bond.valuation_date}
            except ValueError as exc:
                return errors + [str(exc)]
            actual = {f.payment_date for f in coupons if f.payment_date > bond.valuation_date}
            if expected != actual or anchor != bond.maturity_date:
                errors.append("Неполный или нерегулярный график: купонные даты не соответствуют частоте до погашения. Уточните даты или отметьте нестандартные периоды и введите фактические суммы.")
            expected_coupon = bond.face_value * bond.coupon_rate_pct / 100 / bond.coupon_frequency if bond.face_value and math.isfinite(bond.face_value) else None
            if expected_coupon is not None and any(abs(f.coupon_amount - expected_coupon) > max(0.01, expected_coupon * 1e-8) for f in coupons if f.payment_date > bond.valuation_date):
                errors.append("Сумма регулярного купона не соответствует номиналу, ставке и частоте.")
    return errors


CRITICAL_FIELDS = ("face_value", "quantity", "ytm_pct", "coupon_rate_pct", "maturity_date", "coupon_frequency", "day_count")


def scenario_coverage_errors(bond: Bond) -> list[str]:
    """Check explicit scenario dates (including zero coupons), never infer amounts."""
    if not bond.valuation_date or not bond.maturity_date or bond.maturity_date <= bond.valuation_date:
        return []  # General validation reports missing/invalid dates.
    actual = {f.payment_date for f in bond.cashflows if f.payment_date > bond.valuation_date}
    known = {d for d in bond.user_supplied_coupon_dates if bond.valuation_date < d <= bond.maturity_date}
    if known - actual:
        return ["В сценарии отсутствуют известные даты выплат."]
    if bond.nonstandard_coupon_period:
        if not known:
            return ["Для нестандартного графика укажите даты всех будущих выплат."]
        if actual != known:
            return ["Неполный нестандартный график: даты денежных потоков должны совпадать с явно заданными датами выплат."]
    elif bond.coupon_frequency in (1, 2, 3, 4, 6, 12):
        expected = set(regular_dates(bond.maturity_date, bond.valuation_date, bond.maturity_date, bond.coupon_frequency))
        expected.discard(bond.valuation_date)
        if actual != expected:
            return ["Неполный график: задайте даты всех будущих выплат до погашения, включая нулевые купоны."]
    return []


def confirmed_parameter(bond: Bond, name: str) -> bool:
    meta = bond.provenance.get(name, {})
    value = getattr(bond, name)
    serialized = value.isoformat() if isinstance(value, date) else value
    try:
        checked = parse_date(meta.get("checked_on"))
    except (ValueError, TypeError):
        return False
    return (meta.get("status") == "verified" and bool(str(meta.get("source", "")).strip())
            and bool(checked) and meta.get("value") == serialized)
