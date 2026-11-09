"""Input and portfolio validation. Missing market terms are reported, never filled in."""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from models import Bond, COUPON_TYPES
from day_count_conventions import KASE_30_360
from schedule import schedule_errors, scenario_coverage_errors
from coupon_scenarios import coupon_scenario_confirmed, requires_coupon_scenario
from kase_pricing import ACT365F_PRICING_METHOD, KASE_PRICING_METHOD

DAY_COUNTS = ("Actual/365 Fixed", "Actual/Actual (купонный период)", "30/360 US", "30E/360", KASE_30_360, "")
WEIGHT_MODES = ("По рыночной стоимости", "Равные веса", "По номинальной стоимости")


@dataclass
class ValidationReport:
    errors: list[str]
    warnings: list[str]

    @property
    def valid(self) -> bool:
        return not self.errors


def is_finite_number(value: object) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def is_valid_isin(value: str) -> bool:
    """Validate a newly entered ISIN with ISO 6166 shape and Luhn check digit."""
    code = str(value or "").strip().upper()
    if len(code) != 12 or not code.isalnum():
        return False
    expanded = "".join(ch if ch.isdigit() else str(ord(ch) - 55) for ch in code)
    digits = [int(ch) for ch in expanded]
    total = 0
    for index, digit in enumerate(reversed(digits)):
        value_digit = digit * (2 if index % 2 else 1)
        total += value_digit // 10 + value_digit % 10
    return total % 10 == 0


def validate_bond(bond: Bond, *, require_calculable: bool = False, mode: str | None = None) -> ValidationReport:
    """Check a bond's terms and schedule, including financial model constraints."""
    errors: list[str] = []
    warnings: list[str] = []
    if bond.coupon_type not in COUPON_TYPES:
        errors.append("Неизвестный тип купона.")
    if bond.coupon_type == "unknown":
        warnings.append("Для расчёта нужен полный график будущих денежных потоков.")
    if not bond.name.strip():
        errors.append("Не указано название облигации.")
    if not bond.record_id.strip():
        errors.append("Для позиции нужен внутренний идентификатор записи.")
    if bond.face_value is None:
        warnings.append("Не задан номинал одной облигации.")
    elif not is_finite_number(bond.face_value) or float(bond.face_value) <= 0:
        errors.append("Номинал должен быть конечным положительным числом.")
    if bond.quantity is None:
        warnings.append("Не задано количество бумаг.")
    elif isinstance(bond.quantity, bool) or not isinstance(bond.quantity, int) or bond.quantity <= 0:
        errors.append("Количество бумаг должно быть положительным целым числом.")
    if bond.ytm_pct is None:
        warnings.append("Доходность к погашению YTM не задана.")
    elif not is_finite_number(bond.ytm_pct):
        errors.append("YTM должна быть конечным числом в процентах.")
    if bond.coupon_rate_pct is not None and not is_finite_number(bond.coupon_rate_pct):
        errors.append("Ставка купона должна быть конечным числом.")
    elif bond.coupon_rate_pct is not None and float(bond.coupon_rate_pct) < 0:
        errors.append("Ставка купона не может быть отрицательной для данной модели.")
    if bond.coupon_frequency is None:
        warnings.append("Не задана положительная частота начисления доходности.")
    elif isinstance(bond.coupon_frequency, bool) or not isinstance(bond.coupon_frequency, int) or bond.coupon_frequency <= 0:
        errors.append("Частота купонных выплат должна быть положительным целым числом.")
    elif bond.ytm_pct is not None and is_finite_number(bond.ytm_pct):
        if 1 + (float(bond.ytm_pct) / 100) / bond.coupon_frequency <= 0:
            errors.append("Доходность находится вне области периодической модели: 1 + YTM / m должно быть больше нуля.")
    if bond.valuation_date is None:
        warnings.append("Не задана дата оценки.")
    if bond.maturity_date is None:
        warnings.append("Не задана дата погашения.")
    elif bond.valuation_date and bond.maturity_date <= bond.valuation_date:
        errors.append("Дата погашения должна быть позже даты оценки.")
    if bond.day_count not in DAY_COUNTS:
        errors.append("Неизвестная конвенция расчёта НКД.")
    if bond.pricing_method not in (ACT365F_PRICING_METHOD, KASE_PRICING_METHOD):
        errors.append("Неизвестная методика дисконтирования.")
    elif bond.pricing_method == KASE_PRICING_METHOD and bond.day_count != KASE_30_360:
        errors.append("Для методики KASE выберите конвенцию 30/360 KASE.")
    if bond.pricing_method == KASE_PRICING_METHOD:
        if bond.coupon_type == "fixed" and bond.coupon_rate_pct == 0:
            errors.append("Купонная методика KASE в этой версии не применяется к бескупонной облигации. Выберите ACT/365F и частоту капитализации YTM.")
        elif bond.valuation_date and any(f.payment_date > bond.valuation_date and f.amount == 0 for f in bond.cashflows):
            errors.append("Купонная методика KASE в этой версии не поддерживает нулевые сценарные платежи. Выберите ACT/365F для такого графика.")
    if bond.issue_date and bond.maturity_date and bond.issue_date >= bond.maturity_date:
        errors.append("Дата начала начисления должна предшествовать погашению.")
    if bond.issue_date and bond.valuation_date and bond.valuation_date < bond.issue_date:
        errors.append("Дата оценки предшествует началу начисления.")
    if bond.first_coupon_date and bond.issue_date and bond.first_coupon_date <= bond.issue_date:
        errors.append("Первый купон должен быть позже начала начисления.")
    errors.extend(schedule_errors(bond))

    dates = [flow.payment_date for flow in bond.cashflows]
    duplicates = [d.isoformat() for d, count in Counter(dates).items() if count > 1]
    if duplicates:
        errors.append("Дублируются даты денежных потоков: " + ", ".join(duplicates) + ".")
    for i, flow in enumerate(bond.cashflows):
        for label, value in (("купон", flow.coupon_amount), ("погашение", flow.redemption_amount)):
            if not is_finite_number(value) or float(value) < 0:
                errors.append(f"Поток «{label}» на {flow.payment_date:%d.%m.%Y} должен быть конечным неотрицательным числом.")
        if flow.amount <= 0 and not (flow.amount == 0 and requires_coupon_scenario(bond) and bond.schedule_kind == "scenario"):
            errors.append(f"Денежный поток на {flow.payment_date:%d.%m.%Y} должен быть положительным.")
        if bond.maturity_date and flow.payment_date > bond.maturity_date:
            errors.append(f"Денежный поток {flow.payment_date:%d.%m.%Y} находится после погашения.")
        if i and bond.cashflows[i - 1].payment_date >= flow.payment_date:
            errors.append("Даты денежных потоков должны идти строго по возрастанию.")
    if bond.coupon_type == "fixed" and bond.coupon_rate_pct is not None and bond.coupon_rate_pct > 0 and not any(flow.coupon_amount > 0 for flow in bond.cashflows):
        message = "При положительной ставке купона в графике отсутствуют купонные выплаты."
        (errors if require_calculable or bond.schedule_confirmed else warnings).append(message)
    if bond.maturity_date and bond.cashflows:
        redemption = [f for f in bond.cashflows if f.redemption_amount > 0]
        if len(redemption) != 1 or redemption[0].payment_date != bond.maturity_date:
            errors.append("График должен содержать ровно один поток погашения на дату погашения.")
        elif bond.face_value is None:
            warnings.append("Без номинала нельзя проверить сумму погашения.")
        elif abs(redemption[0].redemption_amount - float(bond.face_value)) > max(0.01, float(bond.face_value) * 1e-8):
            errors.append("Сумма погашения должна совпадать с номиналом; амортизация в этой версии не поддерживается.")
    elif require_calculable:
        warnings.append("Не задан график денежных потоков с суммой погашения.")

    if bond.nonstandard_coupon_period and not bond.stub_accrual_confirmed:
        warnings.append("Для нестандартного купонного периода НКД недоступен без правила начисления.")
    if bond.valuation_date:
        future_flows = [f for f in bond.cashflows if f.payment_date > bond.valuation_date]
        if not future_flows and bond.cashflows:
            errors.append("После даты оценки нет будущих денежных потоков.")
    if require_calculable:
        if requires_coupon_scenario(bond):
            errors.extend(scenario_coverage_errors(bond))
            if not coupon_scenario_confirmed(bond):
                errors.append("Подтвердите полноту сценария будущих выплат перед расчётом.")
        if not bond.day_count:
            errors.append("Для расчёта укажите конвенцию НКД.")
        errors.extend(ytm_date_errors(bond, mode))
        if bond.currency.upper() != "KZT":
            errors.append("Денежные показатели этой версии поддерживаются только в KZT; валютная конвертация не подключена.")
        if mode not in (None, "model", "research"):
            errors.append("Неизвестный режим расчёта.")
        if bond.ytm_pct is None:
            errors.append("Для расчёта отсутствует YTM.")
        if bond.valuation_date is None:
            errors.append("Для расчёта отсутствует дата оценки.")
        if bond.maturity_date is None:
            errors.append("Для расчёта отсутствует дата погашения.")
        if bond.coupon_frequency is None or bond.coupon_frequency <= 0:
            errors.append("Для модели дисконтирования задайте частоту m > 0.")
        if bond.face_value is None or bond.quantity is None:
            errors.append("Для оценки позиции нужны номинал и количество бумаг.")
        if not bond.cashflows:
            errors.append("Для расчёта нужен полный график денежных потоков.")
    return ValidationReport(list(dict.fromkeys(errors)), list(dict.fromkeys(warnings)))


def validate_portfolio(bonds: Iterable[Bond]) -> ValidationReport:
    items = list(bonds)
    errors: list[str] = []
    warnings: list[str] = []
    if not items:
        warnings.append("Портфель пуст.")
    seen_record_ids: set[str] = set()
    for bond in items:
        if bond.record_id in seen_record_ids:
            errors.append(f"Дублируется внутренний идентификатор записи {bond.record_id}.")
        seen_record_ids.add(bond.record_id)
        report = validate_bond(bond)
        errors.extend(f"{bond.name or 'Без названия'}: {item}" for item in report.errors)
        warnings.extend(f"{bond.name or 'Без названия'}: {item}" for item in report.warnings)
    return ValidationReport(errors, warnings)


def issue_data_status(bond: Bond) -> str:
    """Backward-compatible name for the technical calculation readiness label."""
    return calculation_readiness(bond)


def calculation_readiness(bond: Bond, mode: str | None = "model") -> str:
    if validate_bond(bond).errors:
        return "Ошибка ввода"
    if validate_bond(bond, require_calculable=True, mode=mode).errors:
        return "Недостаточно данных"
    return "Готово к расчёту"


def missing_calculation_inputs(bond: Bond) -> list[str]:
    fields = {"ytm_pct": "YTM оценки", "quantity": "количество бумаг",
              "valuation_date": "дата оценки", "face_value": "номинал",
              "maturity_date": "дата погашения",
              "coupon_frequency": "частота купонов"}
    missing = [label for key, label in fields.items() if getattr(bond, key) is None]
    if not bond.cashflows:
        missing.append("полный график денежных потоков")
    if not bond.day_count:
        missing.append("конвенция НКД")
    return missing


def ytm_use_signature(bond: Bond) -> dict:
    return {"ytm_pct": bond.ytm_pct,
            "observation_date": bond.ytm_observation_date.isoformat() if bond.ytm_observation_date else None,
            "valuation_date": bond.valuation_date.isoformat() if bond.valuation_date else None}


def ytm_date_errors(bond: Bond, mode: str | None = "model") -> list[str]:
    return []


def workflow_status(bond: Bond | None, mode: str = "model") -> str:
    if bond is None:
        return "Недостаточно данных"
    return calculation_readiness(bond, mode)


def data_status(bond: Bond, mode: str | None = None) -> str:
    """Backward-compatible name for the technical calculation readiness label."""
    return calculation_readiness(bond, mode)
