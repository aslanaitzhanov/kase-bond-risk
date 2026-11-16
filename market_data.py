"""Market-data boundary. No unverified scraping or fabricated live data is used."""
from __future__ import annotations

from datetime import date


def market_data_policy() -> dict[str, str]:
    """Describe the currently supported source policy for the interface."""
    return {
        "status": "Автоматическое получение данных не подключено",
        "message": "В этой версии нет проверенного официального источника/API, поэтому рыночные данные вводятся пользователем вручную.",
        "requirements": "Для статуса подтверждения укажите документ выпуска/официальный источник и дату проверки.",
    }


def source_metadata_status(source: str, checked_on: date | None) -> str:
    """Classify source metadata without making a claim that the source was independently verified."""
    if source.strip().lower().startswith("подтверждено") and checked_on:
        return "Подтверждено источником (заявлено пользователем)"
    if source.strip() and checked_on:
        return "Введено пользователем; источник и дата указаны"
    if source.strip():
        return "Источник указан, дата проверки отсутствует"
    return "Источник и дата проверки не указаны"
