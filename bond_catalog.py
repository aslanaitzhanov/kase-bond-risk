"""Small reviewed local catalog. No scraping, market quotes or file writes.

Register another entry only after reviewing its reference module. Financial
terms stay in that module; the catalog owns discovery and explicit mutations.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable

from models import Bond
from mum180_0009 import ISIN as MUM_ISIN, apply_reviewed_terms
from hsbkb22 import ISIN as HSBK_ISIN, NAME as HSBK_NAME, apply_reviewed_terms as apply_hsbk_terms


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    isin: str
    apply_terms: Callable[[Bond], Bond]


def normalize_isin(isin: str) -> str:
    return isin.strip().upper()


_ENTRIES = {MUM_ISIN: CatalogEntry("MUM180_0009", MUM_ISIN, apply_reviewed_terms),
            HSBK_ISIN: CatalogEntry(HSBK_NAME, HSBK_ISIN, apply_hsbk_terms)}


def catalog_size() -> int:
    return len(_ENTRIES)


def lookup_bond(isin: str, valuation_date: date | None = None) -> Bond | None:
    """Return an independent draft, without an invented yield or holding size."""
    entry = _ENTRIES.get(normalize_isin(isin))
    if entry is None:
        return None
    return entry.apply_terms(Bond(entry.name, entry.isin, face_value=None,
        quantity=None, ytm_pct=None, valuation_date=valuation_date, included=False))


def find_position(bonds: Iterable[Bond], isin: str) -> Bond | None:
    return next((b for b in bonds if normalize_isin(b.isin) == normalize_isin(isin)), None)


def reviewed_update(bond: Bond) -> Bond:
    entry = _ENTRIES.get(normalize_isin(bond.isin))
    if entry is None:
        raise ValueError("Проверенные сведения не найдены.")
    return entry.apply_terms(bond)


def changed_fields(before: Bond, after: Bond) -> dict:
    old, new = before.to_dict(), after.to_dict()
    return {key: {"before": old[key], "after": new[key]} for key in old if old[key] != new[key]}


def add_catalog_position(bonds: Iterable[Bond], candidate: Bond, *, confirmed: bool) -> list[Bond]:
    items = list(bonds)
    if not confirmed:
        raise ValueError("Подтвердите добавление в портфель.")
    if normalize_isin(candidate.isin) not in _ENTRIES:
        raise ValueError("Проверенные сведения не найдены.")
    if find_position(items, candidate.isin):
        raise ValueError("ISIN уже есть в портфеле. Используйте отдельное обновление с просмотром изменений.")
    if any(b.record_id == candidate.record_id for b in items):
        raise ValueError("Идентификатор позиции уже существует.")
    return copy.deepcopy([*items, candidate])


def update_catalog_position(bonds: Iterable[Bond], record_id: str, *, expected: dict,
                            confirmed: bool) -> list[Bond]:
    """Commit only the previewed position; reject stale previews and duplicates."""
    items = list(bonds)
    if not confirmed:
        raise ValueError("Подтвердите обновление позиции.")
    old = next((b for b in items if b.record_id == record_id), None)
    if old is None or old.to_dict() != expected:
        raise ValueError("Позиция изменилась. Просмотрите обновлённые изменения.")
    if any(b.record_id != record_id and normalize_isin(b.isin) == normalize_isin(old.isin) for b in items):
        raise ValueError("Дублируется ISIN. Сначала устраните дубликаты.")
    updated = reviewed_update(old)
    return copy.deepcopy([updated if b.record_id == record_id else b for b in items])
