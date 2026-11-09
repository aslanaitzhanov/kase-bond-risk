"""Local import/export helpers for CSV, Excel and JSON portfolio files."""
from __future__ import annotations

import csv
import io
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from models import Bond, CashFlow, Portfolio
from validation import validate_portfolio

FIELD_LABELS = {
    "record_id": "Идентификатор записи",
    "is_model": "Модельная запись",
    "issue_date": "Начало начисления", "first_coupon_date": "Первый купон",
    "schedule_kind": "Тип графика", "schedule_status": "Статус графика", "regularity_confirmed": "Регулярность подтверждена", "provenance_json": "Происхождение параметров JSON", "pricing_input_json": "Последний расчёт цены JSON",
    "name": "Название", "isin": "ISIN", "issuer": "Эмитент", "instrument_type": "Тип инструмента",
    "currency": "Валюта", "data_source": "Источник данных", "data_checked_date": "Дата проверки данных",
    "face_value": "Номинал, KZT", "quantity": "Количество", "ytm_pct": "YTM, %",
    "coupon_rate_pct": "Купонная ставка, %", "valuation_date": "Дата оценки", "maturity_date": "Дата погашения",
    "coupon_frequency": "Выплат в год", "day_count": "Конвенция НКД", "pricing_method": "Метод расчёта цены", "schedule_confirmed": "График подтверждён",
    "schedule_source": "Источник графика", "included": "Включена в расчёт", "residual_days_user_reported": "Остаточный срок из исходных данных, дней",
    "notes": "Примечания", "cashflows_json": "Денежные потоки JSON", "user_supplied_coupon_dates_json": "Известные купоны JSON",
    "nonstandard_coupon_period": "Нестандартный купонный период", "stub_accrual_confirmed": "НКД по нестандартному периоду подтверждён",
    "coupon_type": "Тип купона", "ytm_origin": "Происхождение YTM", "ytm_source": "Источник YTM",
    "ytm_observation_date": "Дата наблюдения YTM",
}
ALIASES = {label.lower(): field for field, label in FIELD_LABELS.items()}
ALIASES.update({field.lower(): field for field in FIELD_LABELS})
ALIASES.update({"количество бумаг": "quantity", "купон, %": "coupon_rate_pct", "купонных выплат в год": "coupon_frequency", "дата получения данных": "data_checked_date"})


def bonds_to_frame(bonds: list[Bond], *, localized: bool = False) -> pd.DataFrame:
    """Flatten bonds, preserving dated cash flows in a JSON column."""
    rows: list[dict[str, Any]] = []
    for bond in bonds:
        item = bond.to_dict()
        item["cashflows_json"] = json.dumps(item.pop("cashflows"), ensure_ascii=False)
        item["user_supplied_coupon_dates_json"] = json.dumps(item.pop("user_supplied_coupon_dates"), ensure_ascii=False)
        item["provenance_json"] = json.dumps(item.pop("provenance"), ensure_ascii=False)
        item["pricing_input_json"] = json.dumps(item.pop("pricing_input"), ensure_ascii=False)
        rows.append(item)
    frame = pd.DataFrame(rows)
    if localized:
        frame = frame.rename(columns=FIELD_LABELS)
    return frame


def import_bonds_bytes(filename: str, data: bytes) -> tuple[list[Bond], list[str]]:
    """Read CSV/XLSX/JSON bytes and validate the structure without filling missing values."""
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == ".json":
            payload = json.loads(data.decode("utf-8-sig"))
            portfolio = Portfolio.from_dict(payload)
            bonds = portfolio.bonds
        elif suffix == ".csv":
            decoded = data.decode("utf-8-sig")
            # Detect from the header: a truncated sample may end inside long
            # quoted cash-flow/provenance JSON and fool csv.Sniffer.
            sep = ";"
            for candidate in (";", ",", "\t"):
                header = next(csv.reader(io.StringIO(decoded), delimiter=candidate), [])
                normalized = {ALIASES.get(c.strip().lower(), c.strip()) for c in header}
                if "name" in normalized:
                    sep = candidate
                    break
            frame = pd.read_csv(io.StringIO(decoded), sep=sep, dtype=object)
            bonds = _frame_to_bonds(frame)
        elif suffix in (".xlsx", ".xls"):
            frame = pd.read_excel(io.BytesIO(data), dtype=object)
            bonds = _frame_to_bonds(frame)
        else:
            raise ValueError("Поддерживаются только CSV, XLSX и JSON.")
    except (UnicodeDecodeError, json.JSONDecodeError, pd.errors.ParserError, ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"Не удалось прочитать файл «{filename}»: {exc}") from exc
    if not bonds:
        raise ValueError("Файл не содержит облигаций.")
    report = validate_portfolio(bonds)
    messages = report.errors + report.warnings
    return bonds, messages


def _frame_to_bonds(frame: pd.DataFrame) -> list[Bond]:
    if frame.empty:
        raise ValueError("Таблица в файле пуста.")
    normalized = frame.copy()
    normalized.columns = [ALIASES.get(str(c).strip().lower(), str(c).strip()) for c in normalized.columns]
    if "name" not in normalized:
        raise ValueError("Обязательная колонка: «Название» (name).")
    result: list[Bond] = []
    for row_number, row in normalized.iterrows():
        record = {key: _cell(value) for key, value in row.to_dict().items() if key in FIELD_LABELS}
        flows_json = record.pop("cashflows_json", None)
        if flows_json:
            if isinstance(flows_json, str):
                record["cashflows"] = json.loads(flows_json)
            else:
                record["cashflows"] = flows_json
        else:
            record["cashflows"] = []
        dates_json = record.pop("user_supplied_coupon_dates_json", None)
        provenance_json = record.pop("provenance_json", None)
        pricing_input_json = record.pop("pricing_input_json", None)
        if provenance_json:
            record["provenance"] = json.loads(provenance_json) if isinstance(provenance_json, str) else provenance_json
        if pricing_input_json:
            record["pricing_input"] = json.loads(pricing_input_json) if isinstance(pricing_input_json, str) else pricing_input_json
        if dates_json:
            record["user_supplied_coupon_dates"] = json.loads(dates_json) if isinstance(dates_json, str) else dates_json
        else:
            record["user_supplied_coupon_dates"] = []
        try:
            result.append(Bond.from_dict(record))
        except (ValueError, TypeError, OverflowError, json.JSONDecodeError) as exc:
            raise ValueError(f"Ошибка в строке {row_number + 2}: {exc}") from exc
    return result


def _cell(value: Any) -> Any:
    if pd.isna(value):
        return None
    if isinstance(value, (date,)):
        return value.isoformat()
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime().date().isoformat()
    return value


def csv_bytes(bonds: list[Bond]) -> bytes:
    """Export a semicolon-separated UTF-8 BOM CSV with decimal commas."""
    text = bonds_to_frame(bonds, localized=True).to_csv(index=False, sep=";", decimal=",", lineterminator="\n")
    return ("\ufeff" + text).encode("utf-8")


def excel_bytes(bonds: list[Bond]) -> bytes:
    """Export portfolio inputs and full cash-flow JSON into an XLSX workbook."""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        bonds_to_frame(bonds, localized=True).to_excel(writer, index=False, sheet_name="Портфель")
    return buffer.getvalue()


def results_csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    frame = pd.DataFrame(rows)
    return ("\ufeff" + frame.to_csv(index=False, sep=";", decimal=",", lineterminator="\n")).encode("utf-8")


def results_excel_bytes(rows: list[dict[str, Any]], sheet_name: str = "Результаты") -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, index=False, sheet_name=sheet_name[:31])
    return buffer.getvalue()


def save_portfolio_json(path: str | Path, bonds: list[Bond], assessment_date: date | None = None, **settings: Any) -> Path:
    """Fail closed in the public build; export via portfolio_json_bytes instead."""
    raise ValueError("Запись на диск сервера отключена. Скачайте портфель кнопкой JSON.")


def portfolio_json_bytes(bonds: list[Bond], assessment_date: date | None = None, **settings: Any) -> bytes:
    payload = Portfolio(bonds, assessment_date, **settings).to_dict()
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def import_portfolio_bytes(filename: str, data: bytes) -> Portfolio:
    """JSON restores all session settings; tabular formats restore bond inputs."""
    if Path(filename).suffix.lower() == ".json":
        try:
            return Portfolio.from_dict(json.loads(data.decode("utf-8-sig")))
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"Не удалось прочитать портфель: {exc}") from exc
    bonds, _ = import_bonds_bytes(filename, data)
    dates = {b.valuation_date for b in bonds if b.valuation_date}
    return Portfolio(bonds, dates.pop() if len(dates) == 1 else None)


def _source_frame(filename: str, data: bytes, *, cashflows: bool = False, require_isin: bool = True) -> pd.DataFrame:
    if Path(filename).suffix.lower() == ".xlsx":
        return pd.read_excel(io.BytesIO(data), dtype=object)
    if Path(filename).suffix.lower() != ".csv":
        raise ValueError("Для этой загрузки поддерживаются Excel XLSX и CSV.")
    decoded = data.decode("utf-8-sig")
    required = ({"payment_date", "isin"} if require_isin else {"payment_date"}) if cashflows else {"name"}
    flow_aliases = {"дата выплаты": "payment_date", "дата": "payment_date", "isin": "isin"}
    for sep in (";", ",", "\t"):
        header = next(csv.reader(io.StringIO(decoded), delimiter=sep), [])
        names = {flow_aliases.get(c.strip().lower(), ALIASES.get(c.strip().lower(), c.strip().lower())) for c in header}
        if required <= names:
            return pd.read_csv(io.StringIO(decoded), sep=sep, dtype=object)
    raise ValueError("Не найдены обязательные колонки файла.")


def import_parameter_source_bytes(filename: str, data: bytes) -> list[Bond]:
    """Stage user-owned data for search, without touching portfolio positions.

    The input-field mask distinguishes absent cells from model defaults. This
    function deliberately adds receipt metadata only to the staged copies;
    existing portfolio import/export keeps its lossless round-trip behavior.
    """
    loaded = import_portfolio_bytes(filename, data).bonds
    if Path(filename).suffix.lower() == ".json":
        inputs = json.loads(data.decode("utf-8-sig"))["bonds"]
        masks = [set(row) for row in inputs]
    else:
        frame = _source_frame(filename, data)
        frame.columns = [ALIASES.get(str(c).strip().lower(), str(c).strip()) for c in frame.columns]
        masks = [{key for key, value in row.to_dict().items() if _cell(value) not in (None, "")} for _, row in frame.iterrows()]
    pairs = [(bond, mask) for bond, mask in zip(loaded, masks) if bond.isin.strip()]
    if not pairs:
        raise ValueError("В источнике поиска нужны реальные выпуски с ISIN; модельные записи в каталог поиска не добавляются.")
    seen = set()
    for bond, mask in pairs:
        isin = bond.isin.strip().upper()
        if not isin or isin in seen:
            raise ValueError("В источнике повторяется или отсутствует ISIN.")
        seen.add(isin)
        bond.provenance["user_import"] = {"source": Path(filename).name,
            "imported_at": datetime.now(timezone.utc).isoformat(), "status": "user",
            "input_fields": sorted(mask)}
    return [bond for bond, _ in pairs]


def import_cashflows_bytes(filename: str, data: bytes, expected_isin: str = "", *, allow_zero_coupon: bool = False) -> list[CashFlow]:
    """Read one complete explicit schedule. Every row names the exact ISIN.

    Blank monetary cells are errors; zero is accepted only if explicitly given.
    No missing coupon/redemption amount is inferred or generated.
    """
    # Legacy files may still contain ISIN. It is accepted but never needed to
    # identify the position; the caller already selected it by record_id.
    frame = _source_frame(filename, data, cashflows=True, require_isin=False)
    aliases = {"isin": "isin", "дата": "payment_date", "дата выплаты": "payment_date",
        "купон": "coupon_amount", "купон, ₸": "coupon_amount", "погашение": "redemption_amount",
        "номинал, ₸": "redemption_amount", "купон, kzt": "coupon_amount", "погашение, kzt": "redemption_amount"}
    frame.columns = [aliases.get(str(c).strip().lower(), str(c).strip().lower()) for c in frame.columns]
    required = {"payment_date", "coupon_amount", "redemption_amount"}
    if frame.empty or not required <= set(frame.columns):
        raise ValueError("Нужны строки и колонки payment_date, coupon_amount, redemption_amount.")
    flows = []
    for i, row in frame.iterrows():
        raw = {key: _cell(row[key]) for key in required if key != "isin"}
        if any(raw[key] in (None, "") for key in required - {"isin"}):
            raise ValueError(f"Строка {i + 2}: пропущены дата или суммы; укажите ноль явно, если выплаты нет.")
        flow = CashFlow.from_dict({**raw, "coupon_amount": float(str(raw["coupon_amount"]).replace(" ", "").replace("\u00a0", "").replace(",", ".")),
            "redemption_amount": float(str(raw["redemption_amount"]).replace(" ", "").replace("\u00a0", "").replace(",", ".")),
            "status": "user", "source": Path(filename).name})
        if not all(math.isfinite(amount) and amount >= 0 for amount in (flow.coupon_amount, flow.redemption_amount)) or (flow.amount <= 0 and not allow_zero_coupon):
            raise ValueError(f"Строка {i + 2}: нужны конечные неотрицательные суммы и положительный платёж.")
        flows.append(flow)
    return flows


def starter_bonds() -> list[Bond]:
    """No bundled issue or market data in the public build."""
    return []
