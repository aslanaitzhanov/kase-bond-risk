"""Reviewed KASE reference for KZKD00000717 only; no network or file writes.

Issue terms and the placement observation are separate. An auction yield is
never assigned to Bond.ytm_pct by this module's reference update.
"""
from __future__ import annotations

import copy
from datetime import date

from models import Bond
from schedule import generate_schedule

ISIN = "KZKD00000717"
TERMS_SOURCE = "https://kase.kz/ru/information/news/show/1577684"
PLACEMENT_SOURCE = "https://kase.kz/ru/information/news/show/1577722"
SUMMARY_SOURCE = "https://kase.kz/ru/markets/government-securities"
DAY_COUNT_SOURCE = "https://kase.kz/files/normative_base/bond_yield.pdf"
KASE_30_360 = "30/360 KASE (п. 18)"
CHECKED_ON = date(2026, 10, 18)
MATURITY = date(2028, 12, 10)
TRADE_DATE = date(2026, 10, 13)
SETTLEMENT_DATE = date(2026, 10, 15)
PLACEMENT_KEY = "placement_2026_10_13"


def is_mum180_0009(bond: Bond) -> bool:
    return bond.isin.strip().upper() == ISIN


def placement_reference() -> dict:
    """A dated auction observation, not a spot yield input for a portfolio."""
    return {
        "isin": ISIN, "type": "weighted_average_placement",
        "trade_date": TRADE_DATE.isoformat(), "settlement_date": SETTLEMENT_DATE.isoformat(),
        "ytm_pct": 15.2460, "ytm_decimal_places": 4, "ytm_source": SUMMARY_SOURCE,
        "rounded_ytm_pct": 15.25, "rounded_ytm_source": PLACEMENT_SOURCE,
        "clean_price_pct_of_face": 86.0758, "clean_price_source": PLACEMENT_SOURCE,
        "published_remaining_days": 777, "remaining_days_source": TERMS_SOURCE,
        "usage": "Сравнение с размещением; не подставлять автоматически в YTM оценки.",
        "checked_on": CHECKED_ON.isoformat(),
    }


def apply_reviewed_terms(bond: Bond) -> Bond:
    """Return a copy for a NEW starter or an explicit user action on this ISIN.

    Existing yield, quantity and valuation date are preserved; saved portfolios
    are never migrated on import. Cash flows are planned contractual dates,
    without a business-day adjustment or a claim of actual receipt.
    """
    if not is_mum180_0009(bond):
        raise ValueError("Этот справочник применяется только к MUM180_0009 / KZKD00000717.")
    result = copy.deepcopy(bond)
    values = {
        "issuer": "Министерство финансов Республики Казахстан",
        "face_value": 1000.0, "coupon_rate_pct": 7.22, "coupon_frequency": 1,
        "maturity_date": MATURITY, "issue_date": date(2013, 12, 10),
        "day_count": KASE_30_360,
    }
    for key, value in values.items():
        setattr(result, key, value)
        result.provenance[key] = {
            "value": value.isoformat() if isinstance(value, date) else value,
            "status": "verified", "source": DAY_COUNT_SOURCE + "#page=10" if key == "day_count" else TERMS_SOURCE,
            "checked_on": CHECKED_ON.isoformat(),
        }
    result.coupon_type = "fixed"
    result.provenance["coupon_type"] = {"value": "fixed", "status": "model",
        "source": "Фиксированный купон в существующей модели выпуска; ставка и даты сверены с KASE",
        "checked_on": CHECKED_ON.isoformat()}
    result.provenance["coupon_dates_rule"] = {
        "value": {"month": 12, "day": 10, "frequency": 1}, "status": "verified",
        "source": TERMS_SOURCE, "checked_on": CHECKED_ON.isoformat(),
    }
    result.provenance["published_day_count_basis"] = {
        "value": "30/360", "status": "verified", "source": TERMS_SOURCE,
        "checked_on": CHECKED_ON.isoformat(),
        "implementation": "Пункт 18 методики KASE: коррекция 31-го числа; без правила конца февраля US/NASD.",
    }
    result.provenance[PLACEMENT_KEY] = {
        "value": placement_reference(), "status": "verified", "source": PLACEMENT_SOURCE,
        "checked_on": CHECKED_ON.isoformat(),
    }
    result.data_source = "Параметры MUM180_0009 сверены с KASE; доходность оценки задаётся отдельно"
    result.data_checked_date = CHECKED_ON
    result.schedule_source = TERMS_SOURCE
    result.schedule_kind = "regular"
    result.nonstandard_coupon_period = False
    result.stub_accrual_confirmed = False
    result.first_coupon_date = None
    result.cashflows = []
    if result.valuation_date and values["issue_date"] <= result.valuation_date < MATURITY:
        result.cashflows = generate_schedule(result, MATURITY, source=TERMS_SOURCE, status="verified")
    result.schedule_confirmed = bool(result.cashflows)
    result.schedule_status = "verified" if result.cashflows else "user"
    note = "KASE: погашение 10.12.2028; 777 опубликованных расчётных дней не прибавляются как календарные. График плановый, без переноса на рабочие дни. Доходность размещения хранится отдельно."
    if note not in result.notes:
        result.notes = (result.notes + "\n" + note).strip()
    return result


def placement_model_comparison(bond: Bond) -> list[dict]:
    """Diagnostic scenarios with a detached auction yield; never mutate inputs.

    Uses the existing periodic Act/365F discount model unchanged, and the
    reviewed KASE day count for AI. The trade-date case is explicitly hypothetical.
    """
    if not is_mum180_0009(bond):
        raise ValueError("Сравнение доступно только для MUM180_0009.")
    if (bond.maturity_date != MATURITY or bond.face_value != 1000 or bond.coupon_rate_pct != 7.22
            or bond.coupon_frequency != 1 or bond.day_count != KASE_30_360):
        raise ValueError("Сначала примените проверенные параметры MUM180_0009 и сформируйте график.")
    from calculations import bond_analytics

    observation = placement_reference()
    rows = []
    for label, valuation in (("13.10.2026 · условный сценарий", TRADE_DATE),
                             ("15.10.2026 · дата расчётов размещения", SETTLEMENT_DATE)):
        scenario = copy.deepcopy(bond)
        scenario.valuation_date, scenario.ytm_pct, scenario.quantity = valuation, observation["ytm_pct"], 1
        scenario.ytm_origin, scenario.ytm_observation_date = "user_input", None
        scenario.provenance.pop("ytm_date_use", None)
        analytics = bond_analytics(scenario, mode="model")
        clean = analytics["clean_price"]
        if clean is None:
            raise ValueError("Для сравнения clean price нужен корректный график и НКД.")
        reference_clean = scenario.face_value * observation["clean_price_pct_of_face"] / 100
        model_pct = clean / scenario.face_value * 100
        rows.append({
            "Сценарий": label, "Дата оценки модели": valuation.isoformat(),
            "Дата торгов источника": observation["trade_date"], "Дата расчётов источника": observation["settlement_date"],
            "YTM только сценария, %": observation["ytm_pct"],
            "Модельная dirty, ₸": analytics["dirty_price"], "НКД 30/360 KASE, ₸": analytics["accrued_per_bond"],
            "Модельная clean, ₸": clean, "Модельная clean, % номинала": model_pct,
            "Clean размещения, % номинала": observation["clean_price_pct_of_face"],
            "Clean размещения, ₸": reference_clean,
            "Отклонение, п.п. номинала": model_pct - observation["clean_price_pct_of_face"],
            "Отклонение, ₸ / бумага": clean - reference_clean,
            "Модель дисконтирования": "Периодическая YTM; t = фактические дни / 365 (исходная модель)",
        })
    return rows
