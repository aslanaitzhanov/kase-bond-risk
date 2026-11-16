"""Reviewed HSBKb22 terms. Offline reference, never a future-coupon forecast.

Dates in periods are inclusive; payment windows are not actual payment dates.
TCI conversion and issue-specific accrued-interest engine are not implemented.
"""
from __future__ import annotations

import copy
from datetime import date, timedelta

from models import Bond

ISIN = "KZ2C00012482"
NAME = "HSBKb22"
CHECKED_ON = "2026-10-18"
CARD_SOURCE = "https://kase.kz/ru/investors/bonds/HSBKb22/"
TERMS_SOURCE = "https://kase.kz/files/emitters/HSBK/hsbkf9_hsbkb22_2024.pdf"
MARGIN_SOURCE = "https://kase.kz/ru/information/news/show/1533547/"
TCI_SOURCE = "https://kase.kz/files/normative_base/met_indicators_money_market.pdf"
ISSUE_DATE = date(2024, 12, 3)
MATURITY_START, MATURITY_END = date(2027, 12, 3), date(2027, 12, 12)
MATURITY_WINDOW_LABEL = "03–12.12.2027 · договорное окно"
FORMULA_LABEL = "TONIA Compounded (через TONIA Compounded Index, TCI) + 1,25 п.п."
ACCRUAL_UNAVAILABLE = ("Н/Д: точный НКД HSBKb22 по TCI не реализован; нужны значения "
    "TONIA Compounded Index на применимых датах и проверенное правило начисления. "
    "Текущая TONIA, прошлая ставка и пропорция предполагаемого купона не подставляются.")

# Historical reports: publication date, coupon rate, rate report, actual payment,
# publication date of payment report, payment report. No future rates are inferred.
HISTORY = (
    (17.04, "2025-05-29", "1544064", "2025-06-05", "2025-06-05", "1544594"),
    (17.83, "2025-11-26", "1555823", "2025-12-10", "2025-12-11", "1556800"),
    (19.53, "2026-05-28", "1567452", "2026-06-10", "2026-06-10", "1568478"),
)


def is_hsbkb22(bond: Bond) -> bool:
    return bond.isin.strip().upper() == ISIN


def _meta(value, source=TERMS_SOURCE, **extra):
    return {"value": value, "status": "verified", "source": source,
            "checked_on": CHECKED_ON, **extra}


def coupon_periods() -> list[dict]:
    """Contractual dates derived from prospectus; actual payments only from reports."""
    boundaries = [date(2024, 12, 3), date(2025, 6, 3), date(2025, 12, 3),
                  date(2026, 6, 3), date(2026, 12, 3), date(2027, 6, 3), MATURITY_START]
    result = []
    for i, (start, following) in enumerate(zip(boundaries, boundaries[1:])):
        period = {"number": i + 1, "accrual_start": start.isoformat(),
            "accrual_end_inclusive": (following - timedelta(days=1)).isoformat(),
            "index_observation_start": (start - timedelta(days=10)).isoformat(),
            "index_observation_end": (following - timedelta(days=10)).isoformat(),
            "register_date": (following - timedelta(days=1)).isoformat(),
            "payment_window_start": following.isoformat(),
            "payment_window_end": (following + timedelta(days=9)).isoformat(),
            "dates_status": "derived_from_contract", "dates_source": TERMS_SOURCE,
            "rate_pct": None, "rate_publication_date": None, "rate_source": None,
            "actual_payment_date": None, "payment_report_date": None, "payment_source": None}
        if i < len(HISTORY):
            rate, published, news, paid, paid_published, paid_news = HISTORY[i]
            period.update(rate_pct=rate, rate_publication_date=published,
                rate_source=f"https://kase.kz/ru/information/news/show/{news}",
                actual_payment_date=paid, payment_report_date=paid_published,
                payment_source=f"https://kase.kz/ru/information/news/show/{paid_news}")
        result.append(period)
    return result


def apply_reviewed_terms(bond: Bond) -> Bond:
    """Explicit reviewed update; preserve holdings, quotes and user-entered flows."""
    if not is_hsbkb22(bond):
        raise ValueError("Условия HSBKb22 относятся к другому ISIN.")
    result = copy.deepcopy(bond)
    before = result.to_dict()
    if result.coupon_rate_pct is not None:
        result.provenance.setdefault("legacy_coupon_rate_pct", {
            "value": result.coupon_rate_pct, "status": "user", "source": result.data_source,
            "note": "Неподтверждённое историческое сведение пользователя; период и дата наблюдения неизвестны. Не ставка будущих купонов."})
    if result.user_supplied_coupon_dates:
        result.provenance.setdefault("legacy_coupon_dates", {"value": [d.isoformat() for d in result.user_supplied_coupon_dates],
            "status": "user", "source": result.data_source, "note": "Не подтверждены как фактические даты платежа."})
        result.user_supplied_coupon_dates = []
    terms = {"issuer": 'АО «Народный Банк Казахстана»', "face_value": 1000.0,
             "currency": "KZT", "issue_date": ISSUE_DATE, "coupon_type": "floating",
             "coupon_frequency": 2, "day_count": "Actual/365 Fixed"}
    for key, value in terms.items():
        setattr(result, key, value)
        result.provenance[key] = _meta(value.isoformat() if isinstance(value, date) else value,
            CARD_SOURCE if key in ("issuer", "face_value", "currency", "issue_date", "coupon_type", "day_count") else TERMS_SOURCE)
    result.coupon_rate_pct = None
    result.provenance["coupon_rate_pct"] = {"value": None, "status": "unknown", "source": TERMS_SOURCE,
        "checked_on": CHECKED_ON, "note": "Единой постоянной ставки нет; исторические ставки хранятся отдельно."}
    result.provenance["coupon_rule"] = _meta({"label": FORMULA_LABEL, "margin_pp": 1.25,
        "index": "TONIA Compounded Index (TCI)",
        "annualized_index_rule": "(TCI_end / TCI_start - 1) * 365 / dn; затем + 0.0125 в долях годовых",
        "observation_offset_calendar_days": 10, "rate_rounding_decimal_places": 2,
        "implementation": "not_implemented"}, margin_source=MARGIN_SOURCE, index_source=TCI_SOURCE)
    result.provenance["maturity_window"] = _meta({"start": MATURITY_START.isoformat(), "end": MATURITY_END.isoformat()}, CARD_SOURCE)
    result.provenance["coupon_periods"] = _meta(coupon_periods())
    result.provenance["published_day_count_basis"] = _meta("Actual/365", CARD_SOURCE)
    # An exact date, if supplied, is retained solely as a user scenario choice.
    if result.maturity_date:
        previous = copy.deepcopy(result.provenance.get("maturity_date"))
        if previous and previous.get("status") != "model":
            result.provenance.setdefault("legacy_maturity_date", previous)
        result.provenance["maturity_date"] = {"value": result.maturity_date.isoformat(),
            "status": "model", "source": "Дата сценарного платежа пользователя; не фактическое зачисление", "checked_on": CHECKED_ON}
    else:
        result.provenance.pop("maturity_date", None)
    result.data_source, result.data_checked_date = CARD_SOURCE, date.fromisoformat(CHECKED_ON)
    if any(before[key] != result.to_dict()[key] for key in (*terms, "coupon_rate_pct", "user_supplied_coupon_dates")):
        result.schedule_confirmed = False
        result.schedule_status = "user"
    return result


def reviewed_terms_match(bond: Bond) -> bool:
    if not is_hsbkb22(bond):
        return False
    reference = apply_reviewed_terms(Bond(NAME, ISIN))
    keys = ("issuer", "face_value", "currency", "issue_date", "coupon_type", "coupon_frequency", "day_count")
    return (bond.coupon_rate_pct is None and all(getattr(bond, key) == getattr(reference, key)
            and bond.provenance.get(key) == reference.provenance[key] for key in keys)
            and all(bond.provenance.get(key) == reference.provenance[key] for key in ("coupon_rule", "maturity_window")))


def scenario_window_errors(bond: Bond) -> list[str]:
    errors = []
    if bond.maturity_date and not MATURITY_START <= bond.maturity_date <= MATURITY_END:
        errors.append("HSBKb22: сценарная дата погашения должна находиться в договорном окне 03–12.12.2027.")
    if not bond.valuation_date:
        return errors
    windows = [(date.fromisoformat(p["payment_window_start"]), date.fromisoformat(p["payment_window_end"]))
               for p in coupon_periods() if date.fromisoformat(p["payment_window_end"]) > bond.valuation_date]
    dates = [f.payment_date for f in bond.cashflows if f.payment_date > bond.valuation_date]
    if any(not any(start <= d <= end for d in dates) for start, end in windows) or any(
            not any(start <= d <= end for start, end in windows) for d in dates):
        errors.append("HSBKb22: задайте явные платежи для каждого оставшегося договорного окна, включая нулевые купоны. Даты внутри окон — допущение пользователя.")
    return errors


def period_display_rows() -> list[dict]:
    def fmt(value):
        return date.fromisoformat(value).strftime("%d.%m.%Y") if value else "Н/Д"
    return [{"Период": f'{fmt(p["accrual_start"])}–{fmt(p["accrual_end_inclusive"])}',
        "Наблюдение TCI": f'{fmt(p["index_observation_start"])} / {fmt(p["index_observation_end"])}',
        "Реестр": fmt(p["register_date"]),
        "Окно выплаты": f'{fmt(p["payment_window_start"])}–{fmt(p["payment_window_end"])}',
        "Ставка, %": str(p["rate_pct"]) if p["rate_pct"] is not None else "Не определена",
        "Сообщение о ставке": fmt(p["rate_publication_date"]),
        "Фактический платёж": fmt(p["actual_payment_date"]),
        "Сообщение о платеже": fmt(p["payment_report_date"]),
        "Источник ставки": p["rate_source"] or TERMS_SOURCE,
        "Источник платежа": p["payment_source"] or "Не получен"} for p in coupon_periods()]


def terms_display_rows() -> list[dict]:
    reference = apply_reviewed_terms(Bond(NAME, ISIN))
    fields = {"issuer": "Эмитент", "face_value": "Номинал", "currency": "Валюта",
              "issue_date": "Начало обращения", "coupon_type": "Тип купона",
              "coupon_frequency": "Купонных периодов в год", "day_count": "Временная база"}
    rows = [{"Параметр": label, "Значение": str(reference.provenance[key]["value"]),
             "Источник": reference.provenance[key]["source"], "Дата сверки": CHECKED_ON}
            for key, label in fields.items()]
    next(row for row in rows if row["Параметр"] == "Временная база")["Значение"] = "Actual/365 (в модели Actual/365 Fixed); метод НКД по TCI не реализован"
    rows.extend([{"Параметр": "Формула ставки", "Значение": FORMULA_LABEL, "Источник": TERMS_SOURCE, "Дата сверки": CHECKED_ON},
                 {"Параметр": "Погашение", "Значение": MATURITY_WINDOW_LABEL, "Источник": CARD_SOURCE, "Дата сверки": CHECKED_ON}])
    return rows
