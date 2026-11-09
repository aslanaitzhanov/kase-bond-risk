"""Typed domain models for the local KASE bond risk workbench."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import date, datetime
from typing import Any
from uuid import uuid4

COUPON_TYPES = {"unknown": "Неизвестный", "fixed": "Фиксированный", "floating": "Плавающий"}
YTM_ORIGINS = {"user_input": "Модельное допущение", "market": "Биржевые данные", "brokerage": "Брокерские данные", "unspecified": "Не указано"}


def _enum(value, labels, default):
    if value in (None, ""):
        return default
    aliases = {label.casefold(): key for key, label in labels.items()}
    if labels is YTM_ORIGINS:
        aliases.update({"пользовательское допущение": "user_input", "рыночное наблюдение": "market"})
    normalized = str(value).strip().casefold()
    normalized = aliases.get(normalized, normalized)
    if normalized not in labels:
        raise ValueError(f"Неизвестное значение: {value}")
    return normalized


def parse_date(value: Any) -> date | None:
    """Parse a date without silently inventing one."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


@dataclass
class CashFlow:
    """A dated payment; coupon and principal are kept separate to avoid double count."""

    payment_date: date
    coupon_amount: float = 0.0
    redemption_amount: float = 0.0
    status: str = "user"
    source: str = ""

    @property
    def amount(self) -> float:
        return float(self.coupon_amount) + float(self.redemption_amount)

    def to_dict(self) -> dict[str, Any]:
        return {
            "payment_date": self.payment_date.isoformat(),
            "coupon_amount": self.coupon_amount,
            "redemption_amount": self.redemption_amount,
            "status": self.status,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CashFlow":
        if not isinstance(value, dict):
            raise ValueError("Денежный поток должен быть объектом с датой и суммами.")
        payment_date = parse_date(value.get("payment_date") or value.get("date"))
        if payment_date is None:
            raise ValueError("У денежного потока не указана дата выплаты.")
        return cls(
            payment_date=payment_date,
            coupon_amount=float(value.get("coupon_amount", 0) or 0),
            redemption_amount=float(value.get("redemption_amount", 0) or 0),
            status=str(value.get("status", "user")),
            source=str(value.get("source", "")),
        )


@dataclass
class Bond:
    """Bond input data. Calculations require an explicitly confirmed complete schedule."""

    name: str
    isin: str = ""
    record_id: str = field(default_factory=lambda: uuid4().hex)
    issuer: str = ""
    instrument_type: str = "Облигация"
    currency: str = "KZT"
    data_source: str = "Введено пользователем"
    data_checked_date: date | None = None
    face_value: float | None = 1000.0
    quantity: int | None = 20000
    ytm_pct: float | None = None
    coupon_rate_pct: float | None = None
    valuation_date: date | None = None
    maturity_date: date | None = None
    coupon_frequency: int | None = None
    day_count: str = "Actual/365 Fixed"
    cashflows: list[CashFlow] = field(default_factory=list)
    user_supplied_coupon_dates: list[date] = field(default_factory=list)
    nonstandard_coupon_period: bool = False
    stub_accrual_confirmed: bool = False
    schedule_confirmed: bool = False
    schedule_source: str = ""
    included: bool = False
    residual_days_user_reported: int | None = None
    notes: str = ""
    issue_date: date | None = None
    first_coupon_date: date | None = None
    schedule_kind: str = "manual"
    schedule_status: str = "user"
    regularity_confirmed: bool = False
    provenance: dict[str, Any] = field(default_factory=dict)
    coupon_type: str = "unknown"
    ytm_observation_date: date | None = None
    ytm_origin: str = "user_input"
    ytm_source: str = ""
    is_model: bool = False
    # Added at the end to preserve the positional constructor layout of old code.
    pricing_method: str = "Actual/365 Fixed (standard)"
    # Last user-entered price-side inputs, retained with the bond for reruns and
    # JSON portfolio round-trips. This is UI state, not a market data claim.
    pricing_input: dict[str, Any] = field(default_factory=dict)

    @property
    def position_key(self) -> str:
        """Stable position identity independent of user supplied instrument fields."""
        return f"RECORD:{self.record_id}"

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("data_checked_date", "valuation_date", "maturity_date", "issue_date", "first_coupon_date", "ytm_observation_date"):
            result[key] = getattr(self, key).isoformat() if getattr(self, key) else None
        result["cashflows"] = [flow.to_dict() for flow in self.cashflows]
        result["user_supplied_coupon_dates"] = [day.isoformat() for day in self.user_supplied_coupon_dates]
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Bond":
        if not isinstance(value, dict):
            raise ValueError("Запись облигации должна быть объектом.")
        provenance = value.get("provenance") or {}
        if not isinstance(provenance, dict) or any(not isinstance(v, dict) for v in provenance.values()):
            raise ValueError("Происхождение параметров должно содержать объекты метаданных.")
        fields = {
            "name": str(value.get("name") or "").strip(),
            "isin": str(value.get("isin") or "").strip(),
        }
        kwargs: dict[str, Any] = {
            "record_id": str(value.get("record_id", "") or uuid4().hex),
            "is_model": _as_bool(value.get("is_model", False)),
            "issuer": str(value.get("issuer", "") or ""),
            "instrument_type": str(value.get("instrument_type", "Облигация") or ""),
            "currency": str(value.get("currency", "KZT") or "KZT"),
            "data_source": str(value.get("data_source", "Введено пользователем") or "Введено пользователем"),
            "data_checked_date": parse_date(value.get("data_checked_date")),
            "face_value": _optional_float(value.get("face_value")),
            "quantity": _optional_int(value.get("quantity")),
            "ytm_pct": _optional_float(value.get("ytm_pct")),
            "pricing_method": str(value.get("pricing_method", "Actual/365 Fixed (standard)") or "Actual/365 Fixed (standard)"),
            "pricing_input": dict(value.get("pricing_input") or {}),
            "coupon_rate_pct": _optional_float(value.get("coupon_rate_pct")),
            "valuation_date": parse_date(value.get("valuation_date")),
            "maturity_date": parse_date(value.get("maturity_date")),
            "coupon_frequency": _optional_int(value.get("coupon_frequency")),
            "day_count": str(value.get("day_count", "Actual/365 Fixed") or ""),
            "cashflows": [CashFlow.from_dict(x) for x in (value.get("cashflows") or [])],
            "user_supplied_coupon_dates": [parse_date(x) for x in (value.get("user_supplied_coupon_dates") or [])],
            "nonstandard_coupon_period": _as_bool(value.get("nonstandard_coupon_period", False)),
            "stub_accrual_confirmed": _as_bool(value.get("stub_accrual_confirmed", False)),
            "schedule_confirmed": _as_bool(value.get("schedule_confirmed", False)),
            "schedule_source": str(value.get("schedule_source", "") or ""),
            "included": _as_bool(value.get("included", False)),
            "residual_days_user_reported": _optional_int(value.get("residual_days_user_reported")),
            "notes": str(value.get("notes", "") or ""),
            "issue_date": parse_date(value.get("issue_date")),
            "first_coupon_date": parse_date(value.get("first_coupon_date")),
            "schedule_kind": str(value.get("schedule_kind", "manual")),
            "schedule_status": str(value.get("schedule_status", "user")),
            "regularity_confirmed": _as_bool(value.get("regularity_confirmed", False)),
            "provenance": dict(value.get("provenance") or {}),
            "coupon_type": _enum(value.get("coupon_type"), COUPON_TYPES, "unknown"),
            "ytm_observation_date": parse_date(value.get("ytm_observation_date")),
            "ytm_origin": _enum(value.get("ytm_origin"), YTM_ORIGINS, "user_input"),
            "ytm_source": str(value.get("ytm_source", "") or ""),
        }
        return cls(**fields, **kwargs)


def normalize_bond(value: Any) -> Bond:
    """Return a current Bond instance, migrating older in-memory or saved shapes.

    Streamlit can keep objects created by an earlier version of this module in
    session_state after a code reload. Rebuilding through ``from_dict`` applies
    the same defaults and parsing rules used by JSON/CSV/Excel imports.
    """
    if isinstance(value, Bond) and {"pricing_method", "pricing_input"} <= vars(value).keys():
        return value
    if isinstance(value, dict):
        payload = value
    elif is_dataclass(value):
        # Read only fields actually present on the instance. This also handles
        # a partially migrated instance where a newly added dataclass field is
        # absent from its __dict__.
        payload = {item.name: getattr(value, item.name) for item in fields(value)
                   if hasattr(value, item.name)}
    elif hasattr(value, "__dict__"):
        payload = vars(value).copy()
    else:
        raise TypeError("Позиция портфеля должна быть объектом Bond или словарём.")
    if "cashflows" in payload:
        payload["cashflows"] = [flow.to_dict() if hasattr(flow, "to_dict") else flow
                                for flow in (payload["cashflows"] or [])]
    return Bond.from_dict(payload)


@dataclass
class Portfolio:
    bonds: list[Bond] = field(default_factory=list)
    assessment_date: date | None = None
    weight_mode: str = "Равные веса"
    scenarios: list[dict[str, Any]] = field(default_factory=list)
    calculation_mode: str = "model"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "assessment_date": self.assessment_date.isoformat() if self.assessment_date else None,
            "bonds": [bond.to_dict() for bond in self.bonds],
            "weight_mode": self.weight_mode,
            "scenarios": self.scenarios,
            "calculation_mode": self.calculation_mode,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Portfolio":
        if not isinstance(value, dict):
            raise ValueError("Портфель JSON должен быть объектом.")
        if value.get("schema_version", 1) not in (1, 2):
            raise ValueError("Неподдерживаемая версия формата портфеля.")
        items = value.get("bonds")
        if not isinstance(items, list):
            raise ValueError("В JSON-файле должно быть поле bonds со списком облигаций.")
        mode = value.get("calculation_mode", "model")
        weights = value.get("weight_mode", "Равные веса")
        if mode not in ("model", "research") or weights not in ("Равные веса", "По рыночной стоимости", "По номинальной стоимости"):
            raise ValueError("Неизвестный режим расчёта или весов.")
        scenarios = value.get("scenarios", [])
        if not isinstance(scenarios, list) or any(not isinstance(s, dict) or not isinstance(s.get("name"), str) for s in scenarios):
            raise ValueError("Некорректный список сценариев.")
        return cls([Bond.from_dict(item) for item in items], parse_date(value.get("assessment_date")), weights, scenarios, mode)


def _optional_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        value = value.replace("\u00a0", "").replace(" ", "").replace(",", ".")
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    number = float(value)
    if not number.is_integer():
        raise ValueError(f"Ожидалось целое число, получено {value!r}.")
    return int(number)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "да", "y"}
