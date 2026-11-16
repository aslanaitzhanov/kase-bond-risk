"""Offline provider boundary and explicit portfolio commands for ISIN discovery.

No network imports, credentials, saved market-response datasets or dependency
on kase_discovery. Add a future approved provider in the composition factory,
not in app.py. Provider results remain separate; no silent field merging.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable, Protocol
from uuid import uuid4

from bond_catalog import lookup_bond, reviewed_update, normalize_isin, find_position
from models import Bond
from schedule import confirmed_parameter
from hsbkb22 import is_hsbkb22, MATURITY_WINDOW_LABEL, MATURITY_START, MATURITY_END

ISSUE_FIELDS = ("issuer", "instrument_type", "face_value", "currency", "coupon_type", "coupon_rate_pct",
    "maturity_date", "coupon_frequency", "day_count", "issue_date", "first_coupon_date",
    "user_supplied_coupon_dates", "nonstandard_coupon_period", "stub_accrual_confirmed")
PROTECTED = {"record_id", "isin", "name", "quantity", "ytm_pct", "ytm_origin", "ytm_observation_date",
             "ytm_source", "valuation_date", "included", "notes", "ytm_date_use"}


def _fingerprint(bond: Bond) -> str:
    data = bond.to_dict()
    data.pop("record_id")
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]


@dataclass
class BondOffer:
    provider_id: str
    provider_name: str
    bond: Bond
    received_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def token(self) -> str:
        return self.provider_id + ":" + _fingerprint(self.bond)


class BondProvider(Protocol):
    provider_id: str
    name: str
    online: bool

    def lookup(self, isin: str, valuation_date: date | None) -> Bond | None: ...
    def prepare_update(self, current: Bond, source: Bond) -> Bond: ...


class LocalCatalogProvider:
    provider_id, name, online = "local_catalog", "Проверенный локальный каталог", False

    def lookup(self, isin, valuation_date):
        return lookup_bond(isin, valuation_date)

    def prepare_update(self, current, source):
        return reviewed_update(current)


class UserImportProvider:
    provider_id, name, online = "user_import", "Импорт пользователя", False

    def __init__(self, bonds: Iterable[Bond]):
        self.bonds = copy.deepcopy(list(bonds))

    def lookup(self, isin, valuation_date):
        matches = [b for b in self.bonds if normalize_isin(b.isin) == isin]
        if len(matches) > 1:
            raise ValueError("Повторяется ISIN в импортированном источнике; автоматический выбор запрещён.")
        if not matches:
            return None
        result = copy.deepcopy(matches[0])
        mask = set(result.provenance.get("user_import", {}).get("input_fields", []))
        # Defaults inherited from old portfolio formats are not retrieved facts.
        if mask and "day_count" not in mask:
            result.day_count = ""
        if mask and "currency" not in mask:
            result.currency = ""
        if mask and "ytm_origin" not in mask:
            result.ytm_origin = "market" if result.ytm_observation_date else "unspecified"
        result.quantity, result.included = None, False
        return result

    def prepare_update(self, current, source):
        result = copy.deepcopy(current)
        mask = set(source.provenance.get("user_import", {}).get("input_fields", []))
        for key in ISSUE_FIELDS:
            value = getattr(source, key)
            if (not mask or key in mask) and value not in (None, "", []):
                setattr(result, key, copy.deepcopy(value))
        if source.cashflows:
            for key in ("cashflows", "schedule_kind", "schedule_source", "schedule_status", "schedule_confirmed"):
                setattr(result, key, copy.deepcopy(getattr(source, key)))
        elif any(getattr(result, k) != getattr(current, k) for k in ("face_value", "coupon_rate_pct", "maturity_date", "coupon_frequency", "coupon_type")):
            result.schedule_confirmed, result.schedule_status = False, "user"
        for key, meta in source.provenance.items():
            if key not in PROTECTED:
                result.provenance[key] = copy.deepcopy(meta)
        result.data_source = source.provenance.get("user_import", {}).get("source") or source.data_source
        result.data_checked_date = source.data_checked_date
        return result


@dataclass(frozen=True)
class ProviderRegistration:
    provider: BondProvider
    enabled: bool = True
    access_terms_confirmed: bool = False
    access_reference: str = ""


@dataclass
class SearchResult:
    isin: str
    status: str
    offers: list[BondOffer] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class BondSearchService:
    def __init__(self, providers: Iterable[ProviderRegistration]):
        self.providers = {}
        for registration in providers:
            provider = registration.provider
            if not registration.enabled:
                continue
            if provider.online and not (registration.access_terms_confirmed and registration.access_reference.strip()):
                continue  # Never even call an unapproved online provider.
            if provider.provider_id in self.providers:
                raise ValueError("Повторяется идентификатор провайдера.")
            self.providers[provider.provider_id] = provider

    def search(self, isin: str, valuation_date: date | None = None) -> SearchResult:
        isin = normalize_isin(isin)
        result = SearchResult(isin, "not_found" if isin else "invalid_input")
        if not isin:
            return result
        for provider in self.providers.values():
            try:
                bond = provider.lookup(isin, valuation_date)
                if bond is not None:
                    if normalize_isin(bond.isin) != isin:
                        raise ValueError("ISIN ответа не совпадает с запросом.")
                    result.offers.append(BondOffer(provider.provider_id, provider.name, copy.deepcopy(bond)))
            except (ValueError, TypeError, OSError) as exc:
                result.errors.append(f"{provider.name}: {exc}")
        result.status = "found" if result.offers else "source_error" if result.errors else "not_found"
        return result

    def preview_update(self, current: Bond, offer: BondOffer) -> Bond:
        if normalize_isin(current.isin) != normalize_isin(offer.bond.isin):
            raise ValueError("Нельзя обновить позицию данными другого ISIN.")
        provider = self.providers.get(offer.provider_id)
        if provider is None:
            raise ValueError("Источник больше не доступен; повторите поиск.")
        updated = provider.prepare_update(copy.deepcopy(current), copy.deepcopy(offer.bond))
        # Position inputs and market observations cannot be overwritten by an issue source.
        for key in PROTECTED - {"ytm_date_use"}:
            setattr(updated, key, copy.deepcopy(getattr(current, key)))
        for key in ("quantity", "ytm_pct", "ytm_observation_date", "ytm_origin", "ytm_source", "ytm_date_use"):
            if key in current.provenance:
                updated.provenance[key] = copy.deepcopy(current.provenance[key])
            else:
                updated.provenance.pop(key, None)
        return updated

    def commit_update(self, bonds: Iterable[Bond], record_id: str, offer: BondOffer, *, expected: dict, confirmed: bool) -> list[Bond]:
        items = list(bonds)
        current = next((b for b in items if b.record_id == record_id), None)
        if not confirmed:
            raise ValueError("Подтвердите обновление позиции.")
        if current is None or current.to_dict() != expected:
            raise ValueError("Позиция изменилась. Обновите предварительный просмотр.")
        if sum(normalize_isin(b.isin) == normalize_isin(current.isin) for b in items) != 1:
            raise ValueError("Дублируется ISIN.")
        provider = self.providers.get(offer.provider_id)
        if provider is None:
            raise ValueError("Источник отключён. Повторите поиск.")
        fresh = provider.lookup(normalize_isin(offer.bond.isin), offer.bond.valuation_date)
        if fresh is None or _fingerprint(fresh) != _fingerprint(offer.bond):
            raise ValueError("Источник изменился. Повторите поиск и просмотр изменений.")
        updated = self.preview_update(current, offer)
        return copy.deepcopy([updated if b.record_id == record_id else b for b in items])


def default_search_service(imported_bonds: Iterable[Bond] = ()) -> BondSearchService:
    """Composition root. Future approved registrations belong here, never in UI."""
    return BondSearchService([ProviderRegistration(LocalCatalogProvider()),
                              ProviderRegistration(UserImportProvider(imported_bonds))])


def add_found_position(bonds: Iterable[Bond], offer: BondOffer, *, quantity: int,
                       valuation_date: date | None, confirmed: bool) -> list[Bond]:
    items = list(bonds)
    if not confirmed:
        raise ValueError("Подтвердите добавление в портфель.")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError("Введите количество бумаг: положительное целое число.")
    if find_position(items, offer.bond.isin):
        raise ValueError("ISIN уже есть в портфеле. Доступно отдельное обновление.")
    candidate = copy.deepcopy(offer.bond)
    candidate.record_id = uuid4().hex
    candidate.quantity, candidate.valuation_date, candidate.included = quantity, valuation_date, False
    return copy.deepcopy([*items, candidate])


def merge_import_sources(current: Iterable[Bond], incoming: Iterable[Bond]) -> list[Bond]:
    result = copy.deepcopy(list(current))
    for bond in incoming:
        if find_position(result, bond.isin):
            raise ValueError(f"ISIN {bond.isin} уже есть в источнике импорта. Очистите источник перед заменой; портфель сохранится.")
        result.append(copy.deepcopy(bond))
    return result


def parameter_evidence(offer: BondOffer) -> list[dict]:
    bond = offer.bond
    imported = bond.provenance.get("user_import", {})
    rows = []
    for key in ("issuer", "face_value", "currency", "coupon_type", "coupon_rate_pct", "maturity_date",
                "coupon_frequency", "day_count", "cashflows", "ytm_pct", "ytm_observation_date"):
        value = getattr(bond, key)
        meta = bond.provenance.get(key, {})
        if key == "cashflows":
            value = f"{len(bond.cashflows)} выплат" if bond.cashflows else None
            meta = bond.provenance.get("cashflows") or {"source": bond.schedule_source, "checked_on": bond.data_checked_date}
        present = value not in (None, "", "unknown")
        status = "Сверено пользователем с источником" if present and key != "cashflows" and confirmed_parameter(bond, key) else "Ввод пользователя / модель" if present else "Не получено"
        rows.append({"field": key, "value": value.isoformat() if isinstance(value, date) else value,
            "source": meta.get("source") or imported.get("source") or bond.data_source,
            "checked_on": str((meta.get("checked_on") if "checked_on" in meta else bond.data_checked_date) or "Не указана"),
            "received_at": imported.get("imported_at") or offer.received_at, "status": status})
    if is_hsbkb22(bond):
        for key in ("issue_date", "coupon_rule", "maturity_window"):
            meta = bond.provenance.get(key, {})
            value = meta.get("value")
            verified = bool(value and meta.get("status") == "verified" and meta.get("source") and meta.get("checked_on"))
            if key == "issue_date":
                value = bond.issue_date.isoformat() if bond.issue_date else None
                verified = bool(value and confirmed_parameter(bond, key))
            elif key == "coupon_rule" and isinstance(value, dict):
                value = value.get("label")
            elif key == "maturity_window" and value == {"start": MATURITY_START.isoformat(), "end": MATURITY_END.isoformat()}:
                value = MATURITY_WINDOW_LABEL
            rows.append({"field": key, "value": value, "source": meta.get("source", ""),
                "checked_on": meta.get("checked_on", "Не указана"), "received_at": offer.received_at,
                "status": "Сверено с источником" if verified else "Ввод пользователя / модель" if value else "Не получено"})
    return rows
