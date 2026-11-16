"""Isolated KASE public-page discovery prototype, not a licensed market feed.

No dependency on Bond, bond_catalog, Streamlit or financial calculations.
Only public HTML pages are fetched. ng-state ``u`` values are cache identifiers
embedded by KASE in those pages; they are NEVER called as API endpoints.
The CLI requires an explicit snapshot or live mode. --research-live is for a
one-off investigation, not permission to incorporate data into a user product.
KASE information-distribution rules, art. 19, require a contract or written
permission for inclusion in information products / regular redistribution.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

INDEX_URLS = ("https://kase.kz/ru/investors/instruments/bonds",
              "https://kase.kz/ru/investors/instruments/gsecs")
RULES_URL = "https://kase.kz/files/normative_base/info_rules.pdf#page=20"
USER_AGENT = "KASEBondRiskResearch/0.1"
MAX_BYTES = 8_000_000
FIELDS = ("trading_code", "isin", "issuer", "issue_name", "face_value", "currency",
          "coupon_type", "current_coupon_rate_pct", "coupon_rate_expression",
          "coupon_frequency", "coupon_schedule", "maturity_date", "maturity_period",
          "maturity_period_start", "day_count", "day_count_variant", "ytm_pct",
          "market_observation_date", "cashflow_amounts")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_isin(value: str) -> str:
    value = value.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", value):
        raise ValueError("ISIN должен содержать 12 латинских букв/цифр и оканчиваться цифрой.")
    return value


def public_url_allowed(url: str) -> bool:
    p = urlparse(url)
    return (p.scheme == "https" and p.netloc == "kase.kz" and not p.query and not p.fragment
            and bool(re.fullmatch(r"/(?:robots\.txt|ru/investors/instruments/(?:bonds|gsecs)/?|"
                                 r"ru/investors/(?:bonds|gsecs)/[A-Za-z0-9_-]+/?)", p.path)))


@dataclass
class Snapshot:
    url: str
    retrieved_at: str
    body: str = ""
    status: int | None = None
    error: str | None = None
    sha256: str | None = None


class Loader(Protocol):
    def get(self, url: str) -> Snapshot: ...


class SnapshotLoader:
    """Replay original receipt times; missing files never cause a live fallback."""
    def __init__(self, directory: str | Path):
        self.root = Path(directory)
        self.records = {r["url"].rstrip("/"): r for r in json.loads(
            (self.root / "capture_manifest.json").read_text(encoding="utf-8"))}

    def get(self, url: str) -> Snapshot:
        record = self.records.get(url.rstrip("/"))
        if record is None:
            return Snapshot(url, utc_now(), error="snapshot_not_available: нет сохранённого ответа; онлайн-подмена запрещена")
        result = Snapshot(url, record["retrieved_at"], status=record.get("status"), error=record.get("error"))
        if result.error or result.status != 200:
            return result
        try:
            path = (self.root / record["filename"]).resolve()
            if not path.is_relative_to(self.root.resolve()):
                raise ValueError("Путь снимка вне каталога")
            raw = path.read_bytes()
            result.sha256 = hashlib.sha256(raw).hexdigest()
            if result.sha256 != record["sha256"]:
                raise ValueError("Контрольная сумма снимка не совпадает")
            result.body = raw.decode("utf-8")
        except (OSError, ValueError, KeyError) as exc:
            result.error = str(exc)
        return result


class _PublicRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not public_url_allowed(newurl):
            raise URLError("redirect_blocked: перенаправление за пределы публичных страниц")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PublicResearchLoader:
    """Small, sequential, cached research requests; no retries, login or polling."""
    def __init__(self, *, research_only: bool = False, timeout: float = 20):
        if not research_only:
            raise ValueError("Для продукта требуется согласовать права KASE; доступен только явный исследовательский режим.")
        self.timeout = timeout
        self.cache: dict[str, Snapshot] = {}
        self.opener = build_opener(_PublicRedirects())
        self.robots: RobotFileParser | None = None
        self.robots_error: str | None = None
        self.stop_reason: str | None = None

    def _request(self, url: str) -> Snapshot:
        result = Snapshot(url, utc_now())
        try:
            with self.opener.open(Request(url, headers={"User-Agent": USER_AGENT}), timeout=self.timeout) as response:
                result.status = response.status
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError("response_too_large: превышен лимит 8 MB")
                result.sha256 = hashlib.sha256(raw).hexdigest()
                result.body = raw.decode("utf-8")
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            result.status = getattr(exc, "code", result.status)
            result.error = f"load_failed: {exc}"
            if result.status in (401, 403, 429):
                self.stop_reason = f"access_blocked: HTTP {result.status}; дальнейшие запросы прекращены"
        if result.body and "ng-state" not in result.body and re.search(
                r"<title[^>]*>[^<]*(?:captcha|just a moment|access denied)", result.body, re.I):
            result.error = self.stop_reason = "access_challenge: требуется действие человека; обход не выполняется"
        result.retrieved_at = utc_now()
        return result

    def get(self, url: str) -> Snapshot:
        if not public_url_allowed(url):
            return Snapshot(url, utc_now(), error="url_not_allowed: только публичные HTML-страницы KASE")
        if self.stop_reason:
            return Snapshot(url, utc_now(), error=self.stop_reason)
        if self.robots is None and self.robots_error is None:
            response = self._request("https://kase.kz/robots.txt")
            self.cache[response.url] = response
            if response.status in (404, 410):
                self.robots = RobotFileParser()
                self.robots.parse([])  # Absence is not a data licence.
            elif response.status == 200 and not response.error:
                self.robots = RobotFileParser()
                self.robots.parse(response.body.splitlines())
            else:
                self.robots_error = "robots_unavailable: " + str(response.error or response.status)
        if self.robots_error:
            return Snapshot(url, utc_now(), error=self.robots_error)
        if not self.robots.can_fetch(USER_AGENT, url):
            return Snapshot(url, utc_now(), error="robots_disallowed: доступ запрещён robots.txt")
        if self.robots.crawl_delay(USER_AGENT) or self.robots.request_rate(USER_AGENT):
            return Snapshot(url, utc_now(), error="robots_rate_constraint: для этого ограничения нужен отдельный адаптер; запрос не выполнен")
        if url not in self.cache:
            self.cache[url] = self._request(url)
        return self.cache[url]

    def save_snapshots(self, directory: str | Path):
        """Explicit evidence export; not an app portfolio or background cache."""
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        records = []
        for snap in self.cache.values():
            raw = snap.body.encode("utf-8")
            digest = hashlib.sha256(raw).hexdigest()
            filename = digest[:16] + ".html"
            if snap.body:
                (root / filename).write_bytes(raw)
            records.append({"url": snap.url, "retrieved_at": snap.retrieved_at, "status": snap.status,
                            "error": snap.error, "sha256": digest, "filename": filename if snap.body else None})
        (root / "capture_manifest.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


class _Page(HTMLParser):
    def __init__(self, body: str):
        super().__init__(convert_charrefs=True)
        self.state_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._in_state = False
        self._href: str | None = None
        self._link_text: list[str] = []
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and attributes.get("id") == "ng-state":
            self._in_state = True
        if tag == "a":
            self._href, self._link_text = attributes.get("href"), []

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_state = False
        if tag == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._link_text).strip()))
            self._href = None

    def handle_data(self, data):
        if self._in_state:
            self.state_parts.append(data)
        if self._href is not None:
            self._link_text.append(data)

    def state(self) -> dict:
        if not self.state_parts:
            raise ValueError("page_format_changed_or_access_blocked: отсутствует ng-state; не имитируем пустой поиск")
        result = json.loads("".join(self.state_parts))
        if not isinstance(result, dict):
            raise ValueError("page_format_changed: ng-state не является объектом")
        return result


def _payload(state: dict, cache_key: str) -> tuple[Any, str]:
    """Read a cache key from already received HTML. Never make an API request."""
    found = [(v["b"], f"ng-state[{k}].b") for k, v in state.items()
             if isinstance(v, dict) and v.get("u") == cache_key and v.get("s") == 200 and "b" in v]
    if not found:
        return None, ""
    if any(value != found[0][0] for value, _ in found[1:]):
        raise ValueError("conflicting_payloads: противоречивые данные одного раздела")
    return found[0]


def _number(raw: Any) -> float:
    if isinstance(raw, bool) or raw is None:
        raise ValueError("Число отсутствует")
    result = float(str(raw).replace("\u00a0", "").replace(" ", "").replace(",", "."))
    if not math.isfinite(result):
        raise ValueError("Число не является конечным")
    return result


def _text(raw: Any) -> str | None:
    if raw is None:
        return None
    value = html.unescape(str(raw)).strip()
    return None if value in ("", "–", "—", "-") else value


@dataclass
class Parameter:
    value: Any = None
    status: str = "not_available"
    source_url: str | None = None
    retrieved_at: str | None = None
    locator: str | None = None
    observed_at: str | None = None
    note: str = "Не получено; значение не подставляется."
    raw: Any = None


@dataclass
class DiscoveryResult:
    requested_isin: str
    status: str = "source_unavailable"
    parsed_at: str = field(default_factory=utc_now)
    fields: dict[str, Parameter] = field(default_factory=lambda: {key: Parameter() for key in FIELDS})
    errors: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    access: dict = field(default_factory=lambda: {
        "mode": "one_off_research_or_offline_replay", "production_rights": "not_established",
        "rules_url": RULES_URL, "note": "Для включения в продукт и регулярного распространения согласуйте договор/письменное разрешение KASE."})

    def to_dict(self) -> dict:
        return asdict(self)

    def put(self, key, value, source: Snapshot, locator: str, *, status="published_unreviewed", note="Опубликовано KASE; требуется проверка перед применением в модели.", observed_at=None, raw=None):
        self.fields[key] = Parameter(value, status, source.url, source.retrieved_at, locator, observed_at, note, raw)

    def error(self, code: str, url: str, message: str):
        self.errors.append({"code": code, "source_url": url, "message": message, "recorded_at": utc_now()})


def _load(loader: Loader, url: str, result: DiscoveryResult) -> tuple[Snapshot, _Page, dict] | None:
    snap = loader.get(url)
    result.sources.append({"url": url, "retrieved_at": snap.retrieved_at, "http_status": snap.status,
                           "sha256": snap.sha256, "error": snap.error})
    if snap.error or snap.status != 200:
        result.error("load_failed", url, snap.error or f"HTTP {snap.status}")
        return None
    try:
        page = _Page(snap.body)
        return snap, page, page.state()
    except (ValueError, TypeError) as exc:
        result.error("parse_failed", url, str(exc))
        return None


def lookup_isin(isin: str, loader: Loader) -> DiscoveryResult:
    result = DiscoveryResult(isin.strip().upper())
    try:
        isin = normalize_isin(isin)
    except ValueError as exc:
        result.status = "invalid_isin"
        result.error("invalid_isin", "", str(exc))
        return result
    matches = []
    usable_indexes = 0
    for url in INDEX_URLS:
        loaded = _load(loader, url, result)
        if not loaded:
            continue
        snap, page, state = loaded
        try:
            rows, locator = _payload(state, "/api/instruments/securities")
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("ticker"), dict) or not row.get("code") for row in rows):
                raise ValueError("Нет ожидаемого перечня инструментов; это не доказательство отсутствия ISIN")
            usable_indexes += 1
            for i, row in enumerate(rows):
                ticker = row["ticker"]
                if isin not in {str(ticker.get(k, "")).strip().upper() for k in ("nin", "nin2")}:
                    continue
                code = row["code"]
                links = {urljoin(url, href) for href, text in page.links if text == code
                         and public_url_allowed(urljoin(url, href))
                         and re.fullmatch(r"/ru/investors/(?:bonds|gsecs)/" + re.escape(code) + "/?", urlparse(urljoin(url, href)).path)}
                if len(links) != 1:
                    raise ValueError(f"Не найдена однозначная публичная ссылка для {code}")
                matches.append((row, links.pop(), snap, f"{locator}[{i}]"))
        except (ValueError, TypeError, KeyError) as exc:
            result.error("index_format_changed", url, str(exc))
    unique = {(row["code"], url) for row, url, _, _ in matches}
    if not matches:
        result.status = "not_found_in_public_lists" if usable_indexes == len(INDEX_URLS) and not result.errors else "source_unavailable"
        return result
    result.candidates = [{"trading_code": code, "source_url": url} for code, url in sorted(unique)]
    if len(unique) != 1:
        result.status = "ambiguous"
        result.error("multiple_instruments", "", "Один ISIN соответствует нескольким торговым представлениям; автоматический выбор запрещён")
        return result
    row, detail_url, snap, locator = matches[0]
    code = row["code"]
    result.put("trading_code", code, snap, locator + ".code")
    result.put("isin", isin, snap, locator + ".ticker.nin/nin2", status="exact_isin_match", note="Точное совпадение идентификатора, не совпадение названия или торгового кода.")
    for key, raw_key in (("issuer", "org_name_ru"), ("currency", "currency_type")):
        if _text(row.get(raw_key)):
            result.put(key, row[raw_key], snap, locator + "." + raw_key)
    loaded = _load(loader, detail_url, result)
    if not loaded:
        result.status = "partial"
        return result
    try:
        _parse_detail(result, loaded[0], loaded[2], code, isin)
        result.status = "partial" if result.errors else "found_public_data"
    except (ValueError, TypeError, KeyError) as exc:
        # Only index identity survives a failed detail identity/schema check.
        for key in FIELDS:
            if key not in ("trading_code", "isin", "issuer", "currency"):
                result.fields[key] = Parameter(source_url=detail_url, retrieved_at=loaded[0].retrieved_at,
                    status="detail_rejected", note=str(exc))
        result.status = "partial"
        result.error("detail_rejected", detail_url, str(exc))
    return result


def _parse_detail(result: DiscoveryResult, snap: Snapshot, state: dict, code: str, isin: str):
    category = urlparse(snap.url).path.rstrip("/").split("/")[-2]
    primary, primary_loc = _payload(state, f"/api/instruments/{category}/{code}")
    if not isinstance(primary, dict) or primary.get("code") != code or isin not in {
        str(primary.get("ticker", {}).get(k, "")).strip().upper() for k in ("nin", "nin2")}:
        raise ValueError("identity_mismatch: основная карточка не подтверждает запрошенный ISIN")
    attrs, attr_loc = _payload(state, f"/api/instruments/{category}/{code}/characteristics")
    if not isinstance(attrs, dict) or not attrs or any(not isinstance(v, dict) or "label" not in v or "value" not in v for v in attrs.values()):
        raise ValueError("characteristics_missing_or_changed: характеристики не распознаны")
    identifiers = [str(v["value"]).strip().upper() for v in attrs.values() if v["label"] == "ISIN"]
    if not identifiers or any(value != isin for value in identifiers):
        raise ValueError("identity_mismatch: ISIN в характеристиках другой или отсутствует")
    for key in FIELDS:
        if result.fields[key].value is None:
            result.fields[key] = Parameter(source_url=snap.url, retrieved_at=snap.retrieved_at,
                locator=attr_loc, note="Не получено из карточки выпуска; значение не подставляется.")

    def attribute(label: str):
        items = [(v["value"], f"{attr_loc}[{k}].value") for k, v in attrs.items() if v["label"] == label]
        if len(items) > 1 and any(v != items[0][0] for v, _ in items[1:]):
            raise ValueError("conflicting_attributes: " + label)
        return items[0] if items else (None, "")

    for key, label in (("issue_name", "Наименование облигации"), ("currency", "Валюта выпуска и обслуживания"),
                       ("coupon_type", "Вид купонной ставки"), ("day_count", "Расчетный базис (дней в месяце / дней в году)")):
        raw, loc = attribute(label)
        if _text(raw):
            result.put(key, _text(raw), snap, loc)
    result.put("day_count_variant", None, snap, attr_loc, status="requires_document_review",
        note="Базис сохраняется дословно; конкретный алгоритм US/30E/KASE не устанавливается из одной строки базиса.")
    raw, loc = attribute("Номинальная стоимость в валюте выпуска")
    if _text(raw):
        try:
            value = _number(raw)
            if value <= 0:
                raise ValueError("Номинал должен быть положительным")
            result.put("face_value", value, snap, loc, raw=raw)
        except ValueError as exc:
            result.put("face_value", None, snap, loc, raw=raw, status="invalid_source_value", note=str(exc))
            result.error("invalid_face_value", snap.url, str(exc))
    for key, value in attrs.items():
        if ("купонная ставка" in value["label"].lower()) and _text(value["value"]):
            raw, loc = value["value"], f"{attr_loc}[{key}].value"
            result.put("coupon_rate_expression", _text(raw), snap, loc)
            try:
                components = str(raw).split("+")
                amount = sum(_number(c) for c in components)
                result.put("current_coupon_rate_pct", round(amount, 10), snap, loc, raw=raw,
                    status="derived_from_published_components" if len(components) > 1 else "published_unreviewed",
                    note="Последняя опубликованная ставка, не историческая ставка на дату оценки; будущие плавающие купоны не зафиксированы.")
            except ValueError:
                result.put("current_coupon_rate_pct", None, snap, loc, raw=raw, status="unparsed_expression",
                    note="Формула сохранена дословно; числовая ставка не вычисляется из неизвестного индекса.")
    raw, loc = attribute("Дата погашения")
    finish = primary.get("ticker", {}).get("finish_date")
    if raw:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            parsed = date.fromisoformat(raw).isoformat()
        elif finish and date.fromisoformat(finish).strftime("%d.%m.%y") == raw:
            parsed = date.fromisoformat(finish).isoformat()  # Full year from the same issue.
        else:
            parsed = datetime.strptime(raw, "%d.%m.%y").date().isoformat()
        if finish and parsed != finish:
            raise ValueError("maturity_conflict: дата в характеристиках отличается от основной карточки")
        result.put("maturity_date", parsed, snap, loc, raw=raw)
    raw, loc = attribute("Период погашения")
    if raw:
        result.put("maturity_period", _text(raw), snap, loc, raw=raw)
        result.put("maturity_date", None, snap, loc, status="requires_document_review",
            note="Опубликован период погашения, не одна точная дата платежа.", raw=raw)
        if finish:
            result.put("maturity_period_start", date.fromisoformat(finish).isoformat(), snap, primary_loc + ".ticker.finish_date",
                status="published_period_start", note="Начало периода погашения; не подставляется как точная дата платежа.")
    payments, loc = _payload(state, f"/api/instruments/coupon-payments/{code}")
    if isinstance(payments, list) and payments:
        if any(not isinstance(p, dict) or not {"start_date", "end_date", "rate", "total_rate"}.issubset(p) for p in payments):
            raise ValueError("coupon_schedule_changed: неизвестный формат выплат")
        for p in payments:
            date.fromisoformat(p["start_date"])
            if p.get("end_date"):
                date.fromisoformat(p["end_date"])
        result.put("coupon_schedule", payments, snap, loc,
            note="Дословные даты начала/конца выплаты и опубликованные ставки. Это не готовые денежные потоки; будущие ставки плавающего купона могут измениться.")
        dates = sorted({date.fromisoformat(p["start_date"]) for p in payments})
        receipt_date = datetime.fromisoformat(snap.retrieved_at).date()
        previous = [d for d in dates if d <= receipt_date]
        recent = previous[-1:] + [d for d in dates if d > receipt_date]
        steps = {(b.year-a.year)*12+b.month-a.month for a, b in zip(recent, recent[1:]) if a.day == b.day}
        if len(recent) > 1 and len(steps) == 1 and all(a.day == b.day for a, b in zip(recent, recent[1:])):
            step = steps.pop()
            if step in (1, 2, 3, 4, 6, 12):
                result.put("coupon_frequency", 12 // step, snap, loc, status="derived_from_published_schedule",
                    note="Выведено из месячного шага опубликованных ближайших выплат; явного поля частоты нет. Требует сверки с условиями.")
    deals, loc = _payload(state, f"/api/instruments/last-deals/{code}")
    result.put("cashflow_amounts", None, snap, attr_loc, status="requires_document_review",
        note="Суммы купонов/погашения не подставляются по формулам: опубликованный график содержит даты, ставки и периоды выплаты.")
    if isinstance(deals, list):
        valid = []
        for i, deal in enumerate(deals):
            if not isinstance(deal, dict) or deal.get("income") is None or not deal.get("deal_date"):
                continue
            timestamp = datetime.fromisoformat(deal["deal_date"])
            if timestamp.date() > datetime.fromisoformat(snap.retrieved_at).date():
                result.error("future_observation", snap.url, "Дата сделки позже даты получения; запись не принята")
                continue
            if deal.get("volume") is None or _number(deal["volume"]) <= 0:
                continue
            valid.append((timestamp.isoformat(), _number(deal["income"]), i))
        if valid:
            observed = max(v[0] for v in valid)
            latest = [v for v in valid if v[0] == observed]
            if len({v[1] for v in latest}) > 1:
                result.put("ytm_pct", None, snap, loc, status="ambiguous_observation",
                    note="Несколько сделок с одной меткой времени и разной доходностью; автоматический выбор запрещён.")
                result.error("ambiguous_observation", snap.url, "Нельзя однозначно выбрать последнюю доходность")
                return
            observed, income, i = latest[0]
            result.put("ytm_pct", income, snap, f"{loc}[{i}].income", observed_at=observed,
                status="published_last_trade_yield", note="Доходность последней опубликованной сделки, не оценка на выбранную дату и не рыночная оценка API. Часовой пояс времени сделки в ответе не указан.")
            result.put("market_observation_date", observed[:10], snap, f"{loc}[{i}].deal_date", observed_at=observed,
                note="Дата сделки отделена от времени получения страницы; перенос на другую дату запрещён.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Изолированный прототип поиска KASE по точному ISIN")
    parser.add_argument("isin", nargs="+")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--snapshots", type=Path, help="Каталог сохранённых ответов и capture_manifest.json; без сети")
    source.add_argument("--research-live", action="store_true", help="Разовая проверка публичных страниц; не лицензия на промышленное использование")
    parser.add_argument("--output", type=Path, help="Сохранить отчёт JSON по явно указанному пути")
    parser.add_argument("--capture-dir", type=Path, help="Сохранить исходные ответы исследовательского онлайн-запроса")
    args = parser.parse_args()
    loader = SnapshotLoader(args.snapshots) if args.snapshots else PublicResearchLoader(research_only=True)
    results = [lookup_isin(isin, loader).to_dict() for isin in args.isin]
    if args.capture_dir:
        if not isinstance(loader, PublicResearchLoader):
            parser.error("--capture-dir используется только с --research-live")
        loader.save_snapshots(args.capture_dir)
    text = json.dumps(results, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0 if all(r["status"] == "found_public_data" for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
