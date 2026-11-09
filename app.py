"""Калькулятор цены и процентного риска облигаций."""
from __future__ import annotations

import copy
import hashlib
import html
import json
import math
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from calculations import (accrued_for_bond, bond_analytics, day_count_days, ytm_from_price,
                          portfolio_analytics, stress_scenarios)
from kase_pricing import ACT365F_PRICING_METHOD, KASE_PRICING_METHOD
from data_manager import (csv_bytes, excel_bytes, import_portfolio_bytes,
                          portfolio_json_bytes, results_csv_bytes,
                          results_excel_bytes,
                          import_cashflows_bytes)
from models import Bond, CashFlow, Portfolio, parse_date, COUPON_TYPES, normalize_bond
from coupon_scenarios import (requires_coupon_scenario, current_coupon_period,
                              coupon_scenario_confirmed, SCENARIO_NOTE,
                              confirm_coupon_scenario)
from schedule import (CRITICAL_FIELDS, generate_regular_schedule,
                      is_regular_coupon_date, scenario_coverage_errors,
                      schedule_errors)
from validation import (DAY_COUNTS, WEIGHT_MODES, calculation_readiness,
                        missing_calculation_inputs, validate_bond, validate_portfolio,
                        ytm_use_signature)

PAGES = ["Обзор", "Портфель", "Анализ облигаций", "Стресс-тест", "Сценарии", "Методология", "Данные и экспорт"]
_MANUAL_ADD_WIDGET_KEYS = (
    "manual_add_name",
    "manual_add_valuation", "manual_add_maturity", "manual_add_face", "manual_add_currency",
    "manual_add_quantity", "manual_add_coupon_type", "manual_add_coupon_rate",
    "manual_add_frequency", "manual_add_coupon_dates", "manual_add_convention",
    "manual_add_schedule", "manual_add_ytm",
)
_MANUAL_ADD_DEFAULTS = {
    "manual_add_is_model": False,
    "manual_add_valuation": None,
    "manual_add_face": "1000",
    "manual_add_currency": "KZT",
    "manual_add_coupon_type": "unknown",
    "manual_add_frequency": 2,
    "manual_add_convention": DAY_COUNTS[0],
    "manual_add_schedule": "regular",
    "manual_add_ytm_origin": "user_input",
}
PARAM_LABELS = {"face_value": "Номинал", "quantity": "Количество", "ytm_pct": "YTM", "coupon_rate_pct": "Купон",
                "maturity_date": "Погашение", "coupon_frequency": "Частота", "day_count": "Конвенция НКД"}

st.set_page_config(page_title="Процентный риск портфеля облигаций KASE", page_icon="◈", layout="wide")
st.markdown(f"<style>{Path(__file__).with_name('styles.css').read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


def init_state():
    defaults = {"bonds": [], "scenarios": [], "revision": 0, "weight_mode": "Равные веса",
                "calculation_mode": "model", "assessment_date": date.today(), "page": "Обзор"}
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    # Upgrade long-lived Streamlit session objects created before the pricing
    # method became part of Bond. Imports already use Bond.from_dict; this also
    # covers sessions that survive a hot reload of the application code.
    bonds = st.session_state.get("bonds", [])
    if any(not isinstance(bond, Bond) or not {"pricing_method", "pricing_input"} <= vars(bond).keys()
           for bond in bonds):
        st.session_state.bonds = [normalize_bond(bond) for bond in bonds]
    restored = st.session_state.pop("pending_restore", None)
    if restored:
        for key in ("weight_mode", "scenarios", "calculation_mode", "assessment_date"):
            if key in restored and restored[key] is not None:
                st.session_state[key] = restored[key]


def put_bonds(bonds):
    previous = {bond.record_id: bond for bond in
                (normalize_bond(item) for item in st.session_state.get("bonds", []))}
    bonds = [normalize_bond(bond) for bond in bonds]
    dependent_fields = ("ytm_pct", "face_value", "currency", "valuation_date", "maturity_date",
        "coupon_frequency", "coupon_rate_pct", "day_count", "pricing_method", "cashflows", "issue_date",
        "first_coupon_date", "coupon_type", "nonstandard_coupon_period", "stub_accrual_confirmed")
    for bond in bonds:
        old = previous.get(bond.record_id)
        if old is not None and any(getattr(old, field) != getattr(bond, field) for field in dependent_fields):
            st.session_state.pop(f"last_price_calculation_{bond.record_id}", None)
        clean_accrual_changed = (old is not None and bond.pricing_input.get("price_type") == "clean"
            and accrued_for_bond(old)[0] != accrued_for_bond(bond)[0])
        if bond.pricing_input and (not price_input_is_current(bond) or clean_accrual_changed):
            if old is not None and bond.ytm_pct == old.ytm_pct and bond.pricing_input == old.pricing_input:
                # A dependency changed after price inversion. Keep the entered
                # price, but do not present the previous derived yield as current.
                bond.ytm_pct = None
                bond.pricing_input = {**bond.pricing_input, "signature": price_input_signature(bond),
                    "ytm_pct": None, "valuation_date": bond.valuation_date.isoformat() if bond.valuation_date else None,
                    "pricing_method": bond.pricing_method,
                    "error": "Параметры оценки или НКД изменены. Повторите расчёт YTM из введённой цены."}
            else:
                bond.pricing_input = {}
            st.session_state.pop(f"last_price_calculation_{bond.record_id}", None)
    st.session_state.bonds = bonds
    st.session_state.revision += 1
    compute.clear()


def replace_bond(bond):
    put_bonds([bond if b.record_id == bond.record_id else b for b in st.session_state.bonds])


def price_input_signature(bond, *, legacy_integer_face=False):
    """Fingerprint every bond input that can change price inversion."""
    face = float(bond.face_value) if bond.face_value is not None else None
    if legacy_integer_face and face is not None and face.is_integer():
        face = int(face)
    payload = {
        "record_id": bond.record_id,
        "valuation_date": bond.valuation_date.isoformat() if bond.valuation_date else None,
        "pricing_method": bond.pricing_method,
        "day_count": bond.day_count,
        "face_value": face,
        "coupon_frequency": bond.coupon_frequency,
        "issue_date": bond.issue_date.isoformat() if bond.issue_date else None,
        "first_coupon_date": bond.first_coupon_date.isoformat() if bond.first_coupon_date else None,
        "coupon_type": bond.coupon_type,
        "nonstandard_coupon_period": bond.nonstandard_coupon_period,
        "stub_accrual_confirmed": bond.stub_accrual_confirmed,
        "cashflows": [(flow.payment_date.isoformat(), float(flow.coupon_amount),
                        float(flow.redemption_amount)) for flow in bond.cashflows],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def price_input_is_current(bond):
    """Whether persisted price-form inputs still describe this bond and its YTM."""
    saved = bond.pricing_input or {}
    try:
        # Older direct model instances could hash nominal 1000 as an integer;
        # imports normalize it to 1000.0. Both represent the same valuation.
        return (saved.get("signature") in (price_input_signature(bond),
                                           price_input_signature(bond, legacy_integer_face=True))
                and ((saved.get("ytm_pct") is None and bond.ytm_pct is None)
                     or float(saved.get("ytm_pct")) == float(bond.ytm_pct))
                and saved.get("valuation_date") == (bond.valuation_date.isoformat() if bond.valuation_date else None)
                and saved.get("pricing_method") == bond.pricing_method
                and (saved.get("price_type") != "clean" or saved.get("ytm_pct") is None
                     or "accrued_interest_amount" not in saved
                     or saved["accrued_interest_amount"] == accrued_for_bond(bond)[0]))
    except (TypeError, ValueError):
        return False


def sync_ytm_widget_state(state, bond):
    """Keep the editable YTM widget as a view of the record's canonical value.

    Streamlit preserves widget state between reruns. Reconcile it before the
    widget is created whenever the saved bond value changed elsewhere (table,
    price inversion, import/restore, or another card action).
    """
    record_id = bond.record_id
    widget_key = f"yield_value_{record_id}"
    synced_key = f"_yield_widget_model_value_{record_id}"
    canonical = float(bond.ytm_pct) if bond.ytm_pct is not None else None
    marker = state.get(synced_key, object())
    if marker != canonical or widget_key not in state:
        state[widget_key] = canonical
        state[synced_key] = canonical
    return widget_key


def mode():
    # `calculation_mode` remains serialized for backwards compatibility only.
    return "model"


def num(value, digits=3):
    if value is None or not math.isfinite(float(value)):
        return "Н/Д"
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")


def money(value):
    return f"{num(value, 2)} ₸" if value is not None else "Н/Д"


def compact_money(value):
    if value is None:
        return "Н/Д"
    scale, suffix = (1e9, "млрд") if abs(value) >= 1e9 else (1e6, "млн") if abs(value) >= 1e6 else (1, "")
    return f"{num(value / scale, 2)} {suffix} ₸".replace("  ", " ")


def kpis(items):
    cards = "".join(f'<div class="kpi"><label>{html.escape(label)}</label><strong>{html.escape(str(value))}</strong><small>{html.escape(note)}</small></div>' for label, value, note in items)
    st.markdown(f'<div class="kpi-grid">{cards}</div>', unsafe_allow_html=True)


def go_page(name):
    st.session_state.page = name


def empty(title, action="Добавьте параметры и постройте купонный график в разделе «Портфель»."):
    st.markdown(f'<div class="empty">{html.escape(title)}<small>{html.escape(action)}</small></div>', unsafe_allow_html=True)


@st.cache_data(show_spinner=False)
def compute(bonds, weights, calculation_mode):
    return portfolio_analytics(bonds, weights, calculation_mode)


def portfolio():
    return compute(st.session_state.bonds, st.session_state.weight_mode, mode())


def result_for(bond):
    try:
        return bond_analytics(bond, mode())
    except (ValueError, ArithmeticError):
        return None


def issues(bonds):
    reports = [(b, validate_bond(b, require_calculable=True, mode=mode())) for b in bonds]
    rows = [{"Инструмент": b.name,
             "Статус": "Ошибка ввода" if validate_bond(b).errors else "Недостаточно данных" if report.errors else "Готово к расчёту",
             "Что требуется": "; ".join(report.errors)}
            for b, report in reports if report.errors or validate_bond(b).errors]
    if rows:
        with st.expander("Статус расчётов", expanded=False):
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", placeholder="—")


def chart_style(fig, ytitle="Изменение стоимости, %"):
    fig.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Segoe UI, sans-serif", color="#D8E4DC", size=12), height=365,
        margin=dict(l=20, r=20, t=30, b=25), hovermode="x unified",
        legend=dict(orientation="h", y=1.13, x=0, font=dict(size=11)),
        xaxis=dict(title="Изменение YTM, б.п.", gridcolor="#9CB5A812", zerolinecolor="#9CB5A84D"),
        yaxis=dict(title=ytitle, gridcolor="#9CB5A81C", zerolinecolor="#9CB5A84D"))
    return fig


def sensitivity_chart(bonds, key):
    rows = stress_scenarios(bonds, range(-200, 201, 10), calculation_mode=mode(),
                           weight_mode=st.session_state.weight_mode)
    fig = go.Figure()
    for label, field, color, dash in [("Линейное", "linear_change_pct", "#9CB5A8", "dot"),
        ("С выпуклостью", "convexity_change_pct", "#E5C76B", "dash"),
        ("Теоретическая переоценка", "exact_change_pct", "#65CFB2", "solid")]:
        fig.add_trace(go.Scatter(x=[r["shock_bp"] for r in rows], y=[r[field] for r in rows], name=label,
                                mode="lines", line=dict(color=color, width=2, dash=dash), hovertemplate="%{y:.4f}%<extra>%{fullData.name}</extra>"))
    st.plotly_chart(chart_style(fig), width="stretch", key=key)


def bond_record_label(bond):
    return f"{bond.name} · {bond.record_id[:8]}"


def weight_basis_text(weight_mode):
    return {
        "Равные веса": "w = 1 / n среди включённых позиций; для пяти позиций — по 20%.",
        "По номинальной стоимости": "w = номинал × количество / сумму номинальных стоимостей.",
        "По рыночной стоимости": "w = Dirty price × количество / общую стоимость позиций.",
    }[weight_mode]


def snapshot_for_display(value):
    """Hide legacy identification/provenance fields in visible saved snapshots.

    Stored scenarios and downloadable portfolios retain their complete schema.
    """
    hidden = {"isin", "issuer", "data_source", "data_checked_date", "ytm_source", "ytm_origin",
              "ytm_observation_date", "provenance", "schedule_source", "schedule_status", "source", "status", "is_model"}
    if isinstance(value, dict):
        return {key: snapshot_for_display(item) for key, item in value.items() if key not in hidden}
    if isinstance(value, list):
        return [snapshot_for_display(item) for item in value]
    return value


def ytm_price_graph_errors(bond):
    """Require mathematically complete explicit future cash flows before inversion."""
    errors = []
    if not bond.cashflows:
        errors.append("Для расчёта нужен полный график будущих денежных потоков.")
    errors.extend(validate_bond(bond).errors)
    if requires_coupon_scenario(bond):
        errors.extend(scenario_coverage_errors(bond))
        if not coupon_scenario_confirmed(bond):
            errors.append("Подтвердите полноту сценария будущих выплат перед расчётом.")
    elif not bond.nonstandard_coupon_period:
        errors.extend(schedule_errors(bond))
    return list(dict.fromkeys(errors))


def sidebar():
    with st.sidebar:
        st.markdown('<div class="brand"><span>◈</span> KASE Bond Risk</div><div class="eyebrow">Bond calculator</div>', unsafe_allow_html=True)
        st.radio("Навигация", PAGES, key="page", label_visibility="collapsed")
        st.divider()
        st.selectbox("Веса аналитики", WEIGHT_MODES, key="weight_mode", help="Денежный P&L учитывает количество облигаций и рассчитанную dirty price.")
        st.caption(weight_basis_text(st.session_state.weight_mode))
        assessment = st.date_input("Дата оценки", key="assessment_date", format="DD.MM.YYYY")
        if st.button("Применить дату ко всем", width="stretch"):
            updated = copy.deepcopy(st.session_state.bonds)
            for b in updated:
                b.valuation_date = assessment
            put_bonds(updated)
            st.rerun()
        st.caption("Данные текущей сессии · без подключения к котировкам")
        st.caption("Учебный проект, не сервис KASE. Не загружайте персональные или конфиденциальные данные.")


def dashboard():
    bonds, p = st.session_state.bonds, portfolio()
    included = [b for b in bonds if b.included]
    quality = "Готово к расчёту" if p["complete"] else "Недостаточно данных"
    dates = sorted({b.valuation_date for b in included if b.valuation_date})
    dates_text = ", ".join(d.strftime("%d.%m.%Y") for d in dates) or st.session_state.assessment_date.strftime("%d.%m.%Y")
    st.markdown(f'<div class="hero"><div class="eyebrow">Fixed income · KZT</div><h1>KASE Bond Risk</h1><p>Анализ процентного риска портфеля</p><div class="meta"><span>{html.escape(dates_text)}</span><span>{len(bonds)} инструментов · {len(included)} включено</span><span>{quality}</span></div></div>', unsafe_allow_html=True)
    shock = st.slider("Сдвиг YTM, б.п.", -200, 200, 100, 10, key="overview_shock")
    risk = p.get("risk", {})
    r = stress_scenarios(bonds, [shock], calculation_mode=mode(), weight_mode=st.session_state.weight_mode)[0] if p["complete"] else {}
    kpis([("Стоимость портфеля", compact_money(p.get("market_value")), "Теоретическая dirty price"),
          ("Дюрация Маколея", num(risk.get("macaulay_duration_years")), "Лет · выбранные веса"),
          ("Модиф. дюрация", num(risk.get("modified_duration_years")), "Лет · выбранные веса"),
          ("Выпуклость", num(risk.get("convexity")), "Год² · выбранные веса"),
          (f"P&L при {shock:+} б.п.", compact_money(r.get("exact_change_kzt")), "Переоценка фактических позиций")])
    if p["complete"]:
        with st.expander("Точные суммы и стоимость после шока"):
            st.write(f"База: {money(p['market_value'])} · P&L: {money(r['exact_change_kzt'])} · После шока: {money(r['exact_value_kzt'])}")
            st.write(f"Аналитическое изменение при выбранных весах: {num(r['analytical_exact_change_pct'], 4)}%. Денежная переоценка выше использует фактические количества.")
        if p.get("valuation_note"):
            st.info(p["valuation_note"])
    st.subheader("Чувствительность портфеля к изменению доходности")
    if p["complete"]:
        sensitivity_chart(bonds, "overview_curve")
    else:
        empty("График появится после подготовки включённых позиций")
        st.button("Открыть портфель", on_click=go_page, args=("Портфель",), type="primary")
    if bonds:
        st.subheader("Позиции портфеля")
        rows = []
        for b in bonds:
            result = result_for(b) or {}
            rows.append({"Инструмент": bond_record_label(b), "Вес, %": p.get("weights", {}).get(b.position_key, 0) * 100 if p["complete"] else None,
                         "YTM, %": b.ytm_pct if b.ytm_pct is not None else "—",
                         "D_mod, лет": result.get("modified_duration_years"), "Выпуклость": result.get("convexity"),
                         "Включена": b.included, "Расчёт": "Не включена в расчёт" if not b.included else calculation_readiness(b, mode())})
        st.dataframe(pd.DataFrame(rows).style.format(na_rep="—", precision=4), hide_index=True, width="stretch", placeholder="—")
        excluded = [b for b in bonds if not b.included]
        st.caption(f"Исключено: {len(excluded)} · веса нормализуются по {len(included)} включённым позициям. Веса аналитики: {st.session_state.weight_mode}. P&L — по стоимости позиций.")
        if excluded:
            with st.expander("Состав и перенормировка весов"):
                original = {}
                if st.session_state.weight_mode == "Равные веса":
                    original = {b.position_key: 1 / len(bonds) for b in bonds}
                elif st.session_state.weight_mode == "По номинальной стоимости" and all(b.face_value and b.quantity for b in bonds):
                    raw = {b.position_key: b.face_value * b.quantity for b in bonds}
                    total = sum(raw.values())
                    if total > 0:
                        original = {key: value / total for key, value in raw.items()}
                else:
                    all_positions = copy.deepcopy(bonds)
                    for b in all_positions:
                        b.included = True
                    total_result = compute(all_positions, "По рыночной стоимости", mode())
                    original = total_result.get("weights", {})
                st.dataframe(pd.DataFrame([{"Инструмент": bond_record_label(b), "Вес до исключения, %": original[b.position_key] * 100 if b.position_key in original else None,
                    "Вес после исключения, %": p.get("weights", {}).get(b.position_key, 0) * 100 if p["complete"] else None,
                    "Состав": "Включена" if b.included else "Исключена", "Расчёт": calculation_readiness(b, mode())} for b in bonds]).style.format(na_rep="—", precision=4), hide_index=True, width="stretch", placeholder="—")
        if p["errors"] and included:
            with st.expander("Почему итог портфеля недоступен"):
                for error in p["errors"]:
                    st.write(error)
        issues(bonds)


def cashflow_frame(bond, *, include_past=False):
    visible = [f for f in bond.cashflows if include_past or not bond.valuation_date or f.payment_date > bond.valuation_date]
    return pd.DataFrame([{"Дата": f.payment_date, "Тип": "Купон + погашение" if f.coupon_amount and f.redemption_amount else "Погашение" if f.redemption_amount else "Купон",
        "Купон, ₸": f.coupon_amount, "Номинал, ₸": f.redemption_amount, "Всего, ₸": f.amount,
        } for f in visible], columns=["Дата", "Тип", "Купон, ₸", "Номинал, ₸", "Всего, ₸"])


def _date_list(raw):
    values = []
    for token in raw.replace(",", "\n").replace(";", "\n").splitlines():
        if token.strip():
            try:
                parsed = parse_date(token.strip())
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Некорректная купонная дата: {token.strip()}. Используйте ГГГГ-ММ-ДД.") from exc
            if parsed is None:
                raise ValueError(f"Некорректная купонная дата: {token.strip()}. Используйте ГГГГ-ММ-ДД.")
            values.append(parsed)
    if len(values) != len(set(values)):
        raise ValueError("Повторяется купонная дата.")
    return sorted(values)


def _optional_number(raw, label):
    if not str(raw).strip():
        return None
    try:
        value = float(str(raw).replace("\u00a0", "").replace(" ", "").replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"{label}: введите число.") from exc
    if not math.isfinite(value):
        raise ValueError(f"{label}: число должно быть конечным.")
    return value


def cashflow_import_panel(bond):
    with st.expander("Загрузить полный график · Excel / CSV"):
        st.caption("Колонки: payment_date (ГГГГ-ММ-ДД), coupon_amount, redemption_amount. Суммы указываются явно, включая нули.")
        template = "payment_date;coupon_amount;redemption_amount\n"
        st.download_button("Шаблон графика · CSV", ("\ufeff" + template).encode("utf-8"), "cashflows_template.csv", key=f"flows_template_{bond.record_id}")
        uploaded = st.file_uploader("Файл полного графика", type=["csv", "xlsx"], key=f"flows_upload_{bond.record_id}")
        preview_key = f"flows_import_preview_{bond.record_id}"
        if st.button("Предпросмотр импортированного графика", disabled=not uploaded, key=f"flows_preview_{bond.record_id}"):
            try:
                candidate = copy.deepcopy(bond)
                candidate.cashflows = import_cashflows_bytes(
                    uploaded.name, uploaded.getvalue(),
                    allow_zero_coupon=requires_coupon_scenario(bond))
                candidate.user_supplied_coupon_dates = [flow.payment_date for flow in candidate.cashflows]
                candidate.schedule_kind = "scenario" if requires_coupon_scenario(bond) else "manual"
                candidate.schedule_status = "user"
                candidate.schedule_source = ""
                candidate.schedule_confirmed = False
                candidate.provenance.pop("cashflows", None)
                st.session_state[preview_key] = {"before": bond.to_dict(), "candidate": candidate.to_dict()}
            except (ValueError, TypeError, OSError) as exc:
                st.error(str(exc))
        preview = st.session_state.get(preview_key)
        if preview and preview["before"] == bond.to_dict():
            candidate = Bond.from_dict(preview["candidate"])
            st.dataframe(cashflow_frame(candidate), hide_index=True, width="stretch", placeholder="—")
            scenario_confirmed = True
            if requires_coupon_scenario(candidate):
                scenario_confirmed = st.checkbox(
                    "Подтверждаю полноту сценария будущих выплат",
                    key=f"flows_confirm_{bond.record_id}",
                    help="Это подтверждение полноты введённых сумм, а не прогноз или подтверждение будущих рыночных ставок.",
                )
            errors = validate_bond(candidate).errors
            if errors:
                st.error("; ".join(errors))
            if st.button("Сохранить импортированный график", disabled=bool(errors) or not scenario_confirmed, key=f"flows_accept_{bond.record_id}"):
                if requires_coupon_scenario(candidate):
                    candidate = confirm_coupon_scenario(candidate, source="import", confirmed=True)
                replace_bond(candidate)
                st.session_state.pop(preview_key, None)
                st.rerun()


def parameters(bond):
    # Widget identity must follow external position updates, including reference
    # updates. A form's key alone does not reset its child widget state.
    widget = f"parameters_{bond.record_id}_{st.session_state.revision}"
    with st.form(f"parameters_{bond.record_id}_{st.session_state.revision}"):
        a, b, c = st.columns(3)
        name = a.text_input("Название", bond.name, key=f"{widget}_name")
        fields = {}
        for col, key, label in [(a, "face_value", "Номинал, ₸"), (b, "quantity", "Количество"),
                               (a, "coupon_rate_pct", "Купон, %"), (b, "coupon_frequency", "Выплат в год")]:
            fields[key] = col.text_input(label, "" if getattr(bond, key) is None else str(getattr(bond, key)), key=f"{widget}_{key}")
        currency = c.selectbox("Валюта", list(dict.fromkeys([bond.currency, "KZT"])), key=f"{widget}_currency")
        coupon_type = a.selectbox("Тип купона", list(COUPON_TYPES), index=list(COUPON_TYPES).index(bond.coupon_type), format_func=COUPON_TYPES.get, key=f"{widget}_coupon_type")
        for col, key, label in [(a, "valuation_date", "Оценка · ГГГГ-ММ-ДД"), (b, "maturity_date", "Погашение · ГГГГ-ММ-ДД"),
                               (c, "issue_date", "Начало начисления · необязательно"), (a, "first_coupon_date", "Первый купон · необязательно")]:
            fields[key] = col.text_input(label, getattr(bond, key).isoformat() if getattr(bond, key) else "", key=f"{widget}_{key}")
        day_count = b.selectbox("Конвенция НКД", DAY_COUNTS, index=DAY_COUNTS.index(bond.day_count) if bond.day_count in DAY_COUNTS else len(DAY_COUNTS)-1, format_func=lambda value: value or "Не задана", key=f"{widget}_day_count")
        included = c.checkbox("Включить в расчёт", bond.included, key=f"{widget}_included")
        notes = st.text_area("Заметки", bond.notes, height=80, key=f"{widget}_notes")
        save = st.form_submit_button("Сохранить параметры", type="primary")
    if save:
        try:
            payload = bond.to_dict()
            payload.update(fields, name=name.strip(), currency=currency, day_count=day_count, included=included, notes=notes, coupon_type=coupon_type)
            updated = Bond.from_dict(payload)
            if any(updated.to_dict()[key] != bond.to_dict()[key] for key in ("face_value", "coupon_rate_pct", "maturity_date", "coupon_frequency", "issue_date", "first_coupon_date", "coupon_type")):
                updated.schedule_confirmed = False
                updated.schedule_status = "user"
            base = validate_bond(updated)
            identity_errors = validate_portfolio([updated if b.record_id == bond.record_id else b for b in st.session_state.bonds]).errors
            # Existing draft flows can become stale after a term edit; keep them for repair.
            fatal = [e for e in base.errors if "график" not in e.lower() and "купон" not in e.lower() and "поток" not in e.lower() and "погашени" not in e.lower()]
            if fatal:
                raise ValueError("; ".join(fatal or identity_errors))
            replace_bond(updated)
            st.rerun()
        except (ValueError, TypeError, OverflowError) as exc:
            st.error(str(exc))


def schedule_editor(bond):
    st.caption("Укажите даты и суммы будущих выплат. Приложение проверит порядок дат, купонный график и погашение.")
    st.caption("Модель регулярного фиксированного купона: номинал × годовая ставка ÷ число выплат. Изменяемые ставки и нестандартные периоды требуют фактических сумм.")
    if requires_coupon_scenario(bond):
        st.info("Для плавающего или неизвестного купона внесите все будущие суммы вручную. Будущие выплаты по одной ставке не создаются.")
    if bond.maturity_date and bond.valuation_date:
        calendar_days = (bond.maturity_date - bond.valuation_date).days
        try:
            finance_days = day_count_days(bond.valuation_date, bond.maturity_date, bond.day_count)
            st.caption(f"До погашения: {calendar_days} календарных дней · {finance_days} расчётных дней по {bond.day_count}.")
        except ValueError:
            st.caption(f"До погашения: {calendar_days} календарных дней; расчётные дни появятся после выбора конвенции.")

    mode_key = f"schedule_mode_{bond.record_id}"
    default_mode = "regular" if bond.schedule_kind == "regular" or (not bond.cashflows and bond.coupon_type == "fixed") else "manual"
    if mode_key not in st.session_state:
        st.session_state[mode_key] = default_mode
    graph_mode = st.radio("Режим графика", ["regular", "manual"], key=mode_key, horizontal=True,
        format_func=lambda value: "Регулярный график" if value == "regular" else "Полный график вручную")
    cashflow_import_panel(bond)

    if graph_mode == "regular":
        if requires_coupon_scenario(bond):
            st.warning("Автоматический график доступен только для фиксированного регулярного купона.")
            st.button("Предпросмотр графика", disabled=True, key=f"blocked_regular_preview_{bond.record_id}")
        else:
            anchor_default = "\n".join(d.isoformat() for d in bond.user_supplied_coupon_dates)
            with st.expander("Сформировать регулярный график", expanded=not bond.cashflows):
                with st.form(f"generate_{bond.record_id}"):
                    if bond.coupon_rate_pct == 0:
                        st.caption("Бескупонная облигация: только возврат номинала при погашении. Купонные даты не требуются; m задаёт капитализацию YTM.")
                        anchors_text = ""
                    else:
                        anchors_text = st.text_area("Ближайшие известные купонные даты · ГГГГ-ММ-ДД", anchor_default,
                            help="Достаточно одной известной опорной даты. Дополнительные даты проверяются на регулярность.")
                    source = ""
                    status = "user"
                    stub_text, stub_confirmed = "", False
                    try:
                        anchors = _date_list(anchors_text)
                        has_stub = bool(anchors and bond.maturity_date and bond.coupon_frequency and
                            not is_regular_coupon_date(anchors[0], bond.maturity_date, bond.coupon_frequency))
                    except (ValueError, TypeError):
                        anchors, has_stub = [], False
                    if has_stub:
                        st.warning(f"Погашение {bond.maturity_date:%d.%m.%Y} не совпадает с обычной купонной датой. Возможен нестандартный последний период.")
                        stub_text = st.text_input("Фактическая сумма последнего купона по условиям выпуска, ₸")
                        stub_confirmed = True
                    preview = st.form_submit_button("Предпросмотр графика", type="primary")
                preview_key = f"preview_{bond.record_id}"
                if preview:
                    try:
                        anchors = _date_list(anchors_text)
                        explicit = {}
                        if has_stub:
                            amount = _optional_number(stub_text, "Сумма последнего купона")
                            if amount is None or amount < 0:
                                raise ValueError("Введите фактическую неотрицательную сумму последнего купона.")
                            explicit[bond.maturity_date] = amount
                        flows = generate_regular_schedule(bond, anchors, source=source, status=status, stub_amounts=explicit)
                        candidate = copy.deepcopy(bond)
                        candidate.cashflows = flows
                        candidate.user_supplied_coupon_dates = [f.payment_date for f in flows if f.payment_date > bond.valuation_date]
                        candidate.schedule_source = source
                        candidate.schedule_kind = "regular"
                        candidate.schedule_status = status
                        candidate.regularity_confirmed = True
                        candidate.nonstandard_coupon_period = has_stub
                        candidate.schedule_confirmed = False
                        st.session_state[preview_key] = {"before": bond.to_dict(), "candidate": candidate.to_dict()}
                    except (ValueError, TypeError, AttributeError, OverflowError) as exc:
                        st.error(str(exc))

                saved_preview = st.session_state.get(preview_key)
                if saved_preview and saved_preview["before"] == bond.to_dict():
                    candidate = Bond.from_dict(saved_preview["candidate"])
                    st.dataframe(cashflow_frame(candidate), hide_index=True, width="stretch", placeholder="—")
                    report = validate_bond(candidate)
                    for error in report.errors:
                        st.error(error)
                    if st.button("Сохранить график", key=f"accept_{bond.record_id}", disabled=bool(report.errors)):
                        candidate.schedule_confirmed = True
                        candidate.data_checked_date = date.today()
                        replace_bond(candidate)
                        st.session_state.pop(preview_key, None)
                        st.rerun()
        if bond.cashflows:
            st.subheader("Текущие будущие платежи")
            st.dataframe(cashflow_frame(bond), hide_index=True, width="stretch", placeholder="—")

    if graph_mode == "manual":
        st.caption("Введите каждую будущую выплату вручную или загрузите полный CSV/XLSX сценария. Включите нулевые купоны и отдельно укажите погашение номинала.")
        if bond.cashflows:
            st.subheader("Текущие будущие платежи")
            st.dataframe(cashflow_frame(bond), hide_index=True, width="stretch", placeholder="—")
        with st.expander("Редактировать суммы и даты", expanded=not bond.cashflows):
            with st.form(f"flows_form_{bond.record_id}_{st.session_state.revision}"):
                frame = cashflow_frame(bond, include_past=True)[["Дата", "Купон, ₸", "Номинал, ₸"]]
                edited = st.data_editor(frame, num_rows="dynamic", hide_index=True, width="stretch", placeholder="—",
                    column_config={"Дата": st.column_config.DateColumn(format="DD.MM.YYYY", required=True),
                                   "Купон, ₸": st.column_config.NumberColumn(min_value=0.0), "Номинал, ₸": st.column_config.NumberColumn(min_value=0.0)})
                source = ""
                status = "user"
                nonstandard = st.checkbox("Есть нестандартные купонные периоды", bond.nonstandard_coupon_period)
                stub_confirmed = st.checkbox("Задать правило НКД для нестандартного периода", bond.stub_accrual_confirmed)
                scenario_confirmed = True
                if requires_coupon_scenario(bond):
                    scenario_confirmed = st.checkbox(
                        "Подтверждаю полноту сценария будущих выплат",
                        key=f"scenario_confirm_{bond.record_id}_{st.session_state.revision}",
                        help="Сценарные суммы остаются неизменными при сдвиге YTM; это не прогноз будущих ставок.",
                    )
                save = st.form_submit_button("Сохранить график")
            if save:
                try:
                    candidate = copy.deepcopy(bond)
                    flows = []
                    for _, row in edited.iterrows():
                        if pd.isna(row["Дата"]):
                            if any(pd.notna(row[c]) and row[c] != 0 for c in ("Купон, ₸", "Номинал, ₸")):
                                raise ValueError("У потока с суммой отсутствует дата.")
                            continue
                        if any(pd.isna(row[c]) for c in ("Купон, ₸", "Номинал, ₸")):
                            raise ValueError("Укажите обе суммы платежа; если купона или погашения нет, введите 0 явно.")
                        flows.append(CashFlow(parse_date(row["Дата"]), float(row["Купон, ₸"]), float(row["Номинал, ₸"]), status, source))
                    candidate.cashflows = flows
                    candidate.user_supplied_coupon_dates = [flow.payment_date for flow in flows]
                    candidate.schedule_kind = "scenario" if requires_coupon_scenario(bond) else "manual"
                    candidate.regularity_confirmed = False
                    candidate.schedule_source, candidate.data_checked_date = "", None
                    candidate.nonstandard_coupon_period, candidate.stub_accrual_confirmed = nonstandard, stub_confirmed
                    candidate.schedule_confirmed = bool(flows) and not requires_coupon_scenario(candidate)
                    candidate.schedule_status = status
                    if requires_coupon_scenario(candidate):
                        if not scenario_confirmed:
                            raise ValueError("Подтвердите полноту сценария будущих выплат.")
                        candidate = confirm_coupon_scenario(candidate, source="manual", confirmed=True)
                    check = validate_bond(candidate)
                    if check.errors:
                        raise ValueError("; ".join(check.errors))
                    replace_bond(candidate)
                    st.rerun()
                except (ValueError, TypeError, OverflowError) as exc:
                    st.error(str(exc))

def current_period_panel(bond):
    with st.expander("Текущий купонный период · отдельно для НКД"):
        st.caption("Введите даты начала и окончания начисления, сумму купона и конвенцию. НКД считается как доля суммы купона по дням периода.")
        meta = bond.provenance.get("current_coupon_period", {})
        value = meta.get("value", {}) if isinstance(meta.get("value"), dict) else {}
        with st.form(f"current_period_{bond.record_id}_{st.session_state.revision}"):
            a, b = st.columns(2)
            start = a.text_input("Начало периода НКД · ГГГГ-ММ-ДД", str(value.get("start_date") or ""))
            end = b.text_input("Конец периода НКД · ГГГГ-ММ-ДД", str(value.get("end_date") or ""))
            amount = a.text_input("Известная сумма текущего купона / бумага", str(value.get("coupon_amount") if value.get("coupon_amount") is not None else ""))
            convention = b.selectbox("Конвенция текущего периода", DAY_COUNTS, index=DAY_COUNTS.index(value.get("day_count")) if value.get("day_count") in DAY_COUNTS else len(DAY_COUNTS)-1, format_func=lambda v: v or "Не задана")
            save = st.form_submit_button("Сохранить параметры периода")
        if save:
            try:
                candidate = copy.deepcopy(bond)
                candidate.provenance["current_coupon_period"] = {"value": {
                    "isin": bond.isin.strip().upper(), "start_date": parse_date(start).isoformat() if start else None,
                    "end_date": parse_date(end).isoformat() if end else None,
                    "coupon_amount": float(amount.replace(",", ".")) if amount.strip() else None,
                    "day_count": convention, "accrual_method": "coupon_fraction"},
                    "status": "user", "source": "", "checked_on": None}
                accrued, error = accrued_for_bond(candidate)
                if accrued is None:
                    raise ValueError(error)
                replace_bond(candidate)
                st.rerun()
            except (ValueError, TypeError, OverflowError) as exc:
                st.error(str(exc))


def price_panel(bond):
    if requires_coupon_scenario(bond) or accrued_for_bond(bond)[0] is None or "current_coupon_period" in bond.provenance:
        current_period_panel(bond)
    accrued, note = accrued_for_bond(bond)
    result = result_for(bond) or {}
    pricing_key = f"pricing_method_{bond.record_id}"
    pricing_sync_key = f"_pricing_method_model_value_{bond.record_id}"
    if st.session_state.get(pricing_sync_key) != bond.pricing_method or pricing_key not in st.session_state:
        st.session_state[pricing_key] = bond.pricing_method
        st.session_state[pricing_sync_key] = bond.pricing_method
    pricing_options = [ACT365F_PRICING_METHOD]
    if bond.day_count == "30/360 KASE (п. 18)" or bond.pricing_method == KASE_PRICING_METHOD:
        pricing_options.append(KASE_PRICING_METHOD)
    chosen_pricing_method = st.selectbox(
        "Метод расчёта цены",
        pricing_options,
        format_func=lambda value: "ACT/365F · универсальная модель" if value == ACT365F_PRICING_METHOD else "KASE 30/360 · формула п. 11",
        key=pricing_key,
        help="Метод KASE доступен при выбранной базе 30/360 KASE. Старые портфели сохраняют стандартный ACT/365F.",
    )
    if chosen_pricing_method != bond.pricing_method:
        changed = copy.deepcopy(st.session_state.bonds)
        for position in changed:
            if position.record_id == bond.record_id:
                position.pricing_method = chosen_pricing_method
                break
        st.session_state.pop(f"last_price_calculation_{bond.record_id}", None)
        put_bonds(changed)
        st.rerun()
    if bond.pricing_method == KASE_PRICING_METHOD:
        st.caption("Купонная цена дисконтируется по методике KASE, пп. 11–14, 18; НКД рассчитан по 30/360 KASE.")
    a, b, c, d = st.columns(4)
    a.metric("Dirty price", money(result.get("dirty_price")))
    b.metric("НКД / бумага", money(accrued))
    c.metric("Clean price", money(result.get("clean_price")))
    d.metric("НКД / позиция", money(accrued * bond.quantity if accrued is not None and bond.quantity else None))
    st.caption(note)
    if requires_coupon_scenario(bond):
        st.info(SCENARIO_NOTE)
    coupon_dates = sorted(f.payment_date for f in bond.cashflows if f.coupon_amount > 0)
    previous = max((d for d in coupon_dates if bond.valuation_date and d <= bond.valuation_date), default=bond.issue_date)
    following = min((d for d in coupon_dates if bond.valuation_date and d > bond.valuation_date), default=None)
    if not requires_coupon_scenario(bond) and previous and following and bond.valuation_date and bond.day_count and bond.day_count in DAY_COUNTS:
        st.dataframe(pd.DataFrame([{"Предыдущая / начало начисления": previous, "Следующая выплата": following,
            "Дней начисления": day_count_days(previous, bond.valuation_date, bond.day_count),
            "Дней в периоде": day_count_days(previous, following, bond.day_count),
            "База": "365" if bond.day_count == DAY_COUNTS[0] else "Фактический купонный период" if bond.day_count == DAY_COUNTS[1] else "360",
            "Конвенция": bond.day_count}]), hide_index=True, width="stretch", placeholder="—")
    if bond.nonstandard_coupon_period:
        st.caption("Actual/Actual: здесь используется доля суммы заданного купона по фактическим дням. Это не универсальная ICMA-формула для нестандартных периодов.")
    selected_mode = st.session_state.get(f"bond_calculation_mode_{bond.record_id}", "ytm")
    if selected_mode == "ytm":
        frequency = bond.coupon_frequency if isinstance(bond.coupon_frequency, int) and bond.coupon_frequency > 0 else 1
        ytm_widget_key = sync_ytm_widget_state(st.session_state, bond)
        with st.form(f"yield_input_{bond.record_id}_{st.session_state.revision}"):
            entered_ytm = st.number_input("YTM, % годовых", min_value=-100.0 * frequency + 1e-6, max_value=1000.0,
                step=0.01,
                format="%.4f", placeholder="—", key=ytm_widget_key)
            calculate = st.form_submit_button("Рассчитать теоретическую цену", type="primary")
        if calculate:
            try:
                if entered_ytm is None:
                    raise ValueError("Недостаточно данных: укажите YTM или цену.")
                updated = copy.deepcopy(bond)
                updated.ytm_pct = entered_ytm
                if not updated.valuation_date:
                    raise ValueError("Недостаточно данных: укажите дату оценки.")
                report = validate_bond(updated, require_calculable=True, mode="model")
                if report.errors:
                    raise ValueError("Недостаточно данных: " + "; ".join(report.errors))
                st.session_state.pop(f"last_price_calculation_{bond.record_id}", None)
                replace_bond(updated)
                st.rerun()
            except (ValueError, TypeError, OverflowError) as exc:
                st.error(str(exc))
    else:
        graph_errors = ytm_price_graph_errors(bond)
        if graph_errors:
            st.info("Недостаточно данных: сформируйте купонный график. " + "; ".join(graph_errors))
        result_key = f"last_price_calculation_{bond.record_id}"
        stored_result = bond.pricing_input if price_input_is_current(bond) else {}
        revision = st.session_state.revision
        with st.form(f"yield_from_price_{bond.record_id}_{st.session_state.revision}"):
            first, second, third = st.columns(3)
            observed_price = first.number_input("Цена одной облигации", min_value=0.0001,
                value=stored_result.get("price"), step=0.0001, format="%.4f", placeholder="—",
                key=f"price_value_{bond.record_id}_{revision}")
            price_type = second.selectbox("Тип цены", ["clean", "dirty"],
                index=1 if stored_result.get("price_type") == "dirty" else 0,
                format_func=lambda v: "Clean · без НКД" if v == "clean" else "Dirty · с НКД",
                key=f"price_type_{bond.record_id}_{revision}")
            price_unit = third.selectbox("Единицы цены", ["KZT", "percent"],
                index=1 if stored_result.get("price_unit") == "percent" else 0,
                format_func=lambda v: "Тенге за бумагу" if v == "KZT" else "% номинала",
                key=f"price_unit_{bond.record_id}_{revision}")
            calculate = st.form_submit_button("Рассчитать YTM", type="primary", disabled=bool(graph_errors))
        input_matches_result = bool(stored_result and observed_price is not None
                and float(observed_price) == float(stored_result.get("price", math.nan))
                and price_type == stored_result.get("price_type")
                and price_unit == stored_result.get("price_unit")
                and stored_result.get("ytm_pct") is not None
                and stored_result.get("error") is None)
        if input_matches_result:
            st.caption(f"Введено: {num(stored_result['price'], 4)} {stored_result['unit_label']} · "
                f"{stored_result['price_type_label']} · YTM рассчитана: {num(bond.ytm_pct, 4)}% · "
                f"дата оценки {bond.valuation_date:%d.%m.%Y}")
        elif stored_result and stored_result.get("error"):
            st.error(stored_result["error"])
        if calculate:
            try:
                price_value = observed_price
                if price_value is None:
                    raise ValueError("Недостаточно данных: укажите YTM или цену.")
                original_price = price_value
                if price_unit == "percent":
                    if not bond.face_value or bond.face_value <= 0:
                        raise ValueError("Недостаточно данных: укажите положительный номинал для перевода цены из процента номинала.")
                    price_value = price_value * bond.face_value / 100
                if not bond.valuation_date:
                    raise ValueError("Недостаточно данных: укажите дату оценки.")
                accrued, accrued_note = accrued_for_bond(bond)
                if price_type == "clean" and accrued is None:
                    raise ValueError("Для Clean price нужен НКД: " + accrued_note)
                result = ytm_from_price(bond.cashflows, bond.valuation_date, price_value, bond.coupon_frequency,
                    price_type=price_type, accrued_interest_amount=accrued or 0.0,
                    pricing_method=bond.pricing_method, day_count=bond.day_count)
                updated = copy.deepcopy(bond)
                updated.ytm_pct = result
                updated.pricing_input = {
                    "price": float(original_price), "price_type": price_type,
                    "price_unit": price_unit,
                    "unit_label": "% номинала" if price_unit == "percent" else "₸",
                    "price_type_label": "Clean" if price_type == "clean" else "Dirty",
                    "valuation_date": bond.valuation_date.isoformat(),
                    "pricing_method": bond.pricing_method,
                    "signature": price_input_signature(bond), "ytm_pct": float(result),
                    "accrued_interest_amount": accrued if price_type == "clean" else None,
                }
                replace_bond(updated)
                st.session_state.pop(result_key, None)
                st.rerun()
            except (ValueError, TypeError, OverflowError) as exc:
                if not input_matches_result and observed_price is not None:
                    # Do not leave the previous inverse result looking current
                    # for a newly selected price kind/unit/amount that failed.
                    updated = copy.deepcopy(bond)
                    updated.ytm_pct = None
                    updated.pricing_input = {
                        "price": float(observed_price), "price_type": price_type,
                        "price_unit": price_unit,
                        "unit_label": "% номинала" if price_unit == "percent" else "₸",
                        "price_type_label": "Clean" if price_type == "clean" else "Dirty",
                        "valuation_date": bond.valuation_date.isoformat() if bond.valuation_date else None,
                        "pricing_method": bond.pricing_method,
                        "signature": price_input_signature(bond), "ytm_pct": None,
                        "error": str(exc),
                    }
                    replace_bond(updated)
                    st.rerun()
                st.error(str(exc))


def risk_panel(bond):
    r = result_for(bond)
    if not r:
        empty("Метрики недоступны: исправьте параметры или график")
        return
    a, b, c = st.columns(3)
    a.metric("Маколей, лет", num(r["macaulay_duration_years"]))
    b.metric("D_mod, лет", num(r["modified_duration_years"]))
    c.metric("Выпуклость, год²", num(r["convexity"]))
    single = copy.deepcopy(bond)
    single.included = True
    sensitivity_chart([single], f"bond_curve_{bond.record_id}")
    with st.expander("Проверка производных конечными разностями"):
        st.dataframe(pd.DataFrame([{"Метрика": label, "Аналитически": r[field], "Численно": r["numerical"][numeric], "Абс. разница": r[diff]}
            for label, field, numeric, diff in [("D_mod", "modified_duration_years", "numerical_modified_duration", "duration_abs_diff"),
                ("Выпуклость", "convexity", "numerical_convexity", "convexity_abs_diff")]]), hide_index=True, width="stretch", placeholder="—")


def bond_card(bond, editable=True):
    st.subheader(bond_record_label(bond))
    mode_key = f"bond_calculation_mode_{bond.record_id}"
    previous_mode_key = f"previous_calculation_mode_{bond.record_id}"
    st.session_state.setdefault(mode_key, "price" if price_input_is_current(bond) else "ytm")
    st.session_state.setdefault(previous_mode_key, st.session_state[mode_key])
    def switch_mode():
        selected = st.session_state[mode_key]
        previous = st.session_state.get(previous_mode_key, "ytm")
        st.session_state[previous_mode_key] = selected
        if selected != previous:
            st.session_state.pop(f"last_price_calculation_{bond.record_id}", None)
    st.radio("Режим оценки", ["ytm", "price"], key=mode_key, horizontal=True,
        format_func=lambda v: "Знаю YTM" if v == "ytm" else "Знаю цену",
        on_change=switch_mode)
    state = "Ошибка ввода" if validate_bond(bond).errors else "Готово к расчёту"
    if not bond.cashflows:
        state = "Недостаточно данных: сформируйте купонный график"
    elif bond.ytm_pct is None:
        state = "Недостаточно данных: укажите YTM или цену"
    elif validate_bond(bond, require_calculable=True, mode=mode()).errors:
        state = "Недостаточно данных"
    st.caption(state)
    if not bond.included:
        st.caption("Не включена в расчёт")
    ytm_value = f"{num(bond.ytm_pct, 4)}%" if bond.ytm_pct is not None else "—"
    st.caption(f"Купон: {COUPON_TYPES.get(bond.coupon_type)} · YTM: {ytm_value}")
    if bond.maturity_date and bond.valuation_date:
        actual_days = (bond.maturity_date - bond.valuation_date).days
        try:
            convention_days = day_count_days(bond.valuation_date, bond.maturity_date, bond.day_count)
            st.caption(f"Оценка: {bond.valuation_date:%d.%m.%Y} · Погашение: {bond.maturity_date:%d.%m.%Y} · {actual_days} календарных дней / {convention_days} расчётных дней ({bond.day_count}).")
        except ValueError:
            st.caption(f"Оценка: {bond.valuation_date:%d.%m.%Y} · Погашение: {bond.maturity_date:%d.%m.%Y} · {actual_days} календарных дней.")
    price_panel(bond)
    tabs = st.tabs(["Параметры", "Купонный график", "Риск"])
    with tabs[0]:
        parameters(bond)
    with tabs[1]:
        schedule_editor(bond)
    with tabs[2]:
        risk_panel(bond)
    issues([bond])


def portfolio_page():
    st.title("Портфель")
    st.caption("Введите параметры облигации и полный график выплат для выбранной даты оценки.")
    with st.expander("Добавить облигацию", expanded=st.session_state.get("manual_add_open", not st.session_state.bonds)):
        st.caption("Цена и YTM вводятся в карточке облигации после добавления.")
        draft_data = st.session_state.get("manual_add_draft")
        if not draft_data:
            remembered = st.session_state.get("manual_add_values", {})
            for key, value in remembered.items():
                if key not in st.session_state:
                    st.session_state[key] = value
        if draft_data:
            draft = Bond.from_dict(draft_data)
            st.info(f"Карточка облигации · {draft.name}")
            st.dataframe(pd.DataFrame([{
                "Название": draft.name, "Дата оценки": draft.valuation_date,
                "Погашение": draft.maturity_date, "Номинал": draft.face_value, "Валюта": draft.currency,
                "Количество": draft.quantity, "Тип купона": COUPON_TYPES.get(draft.coupon_type),
                "Ставка купона, %": draft.coupon_rate_pct, "Выплат в год": draft.coupon_frequency,
                "YTM, %": draft.ytm_pct,
            }]), hide_index=True, width="stretch", placeholder="—")
            confirm_col, edit_col = st.columns(2)
            if confirm_col.button("Подтвердить и добавить в портфель", type="primary", key="manual_add_confirm"):
                errors = validate_portfolio([*st.session_state.bonds, draft]).errors
                if errors:
                    st.error("; ".join(errors))
                else:
                    put_bonds([*st.session_state.bonds, draft])
                    st.session_state.pop("manual_add_draft", None)
                    for key in _MANUAL_ADD_WIDGET_KEYS:
                        st.session_state.pop(key, None)
                    st.session_state.pop("manual_add_values", None)
                    st.rerun()
            if edit_col.button("Изменить данные", key="manual_add_edit"):
                st.session_state.pop("manual_add_draft", None)
                st.rerun()
        else:
            for key, default in _MANUAL_ADD_DEFAULTS.items():
                if key not in st.session_state:
                    st.session_state[key] = st.session_state.assessment_date if default is None else default
            with st.form("manual_add_form_v2", clear_on_submit=False):
                a, b, c = st.columns(3)
                name = a.text_input("Название *", key="manual_add_name")
                d, e = st.columns(2)
                valuation = d.date_input("Дата оценки", format="DD.MM.YYYY", key="manual_add_valuation")
                maturity_text = e.text_input("Дата погашения · ГГГГ-ММ-ДД *", placeholder="2030-06-15", key="manual_add_maturity")
                with st.expander("Условия выпуска и оценка"):
                    x, y, z = st.columns(3)
                    face_text = x.text_input("Номинал одной облигации *", key="manual_add_face")
                    currency = y.text_input("Валюта", max_chars=3, key="manual_add_currency").upper()
                    quantity_text = z.text_input("Количество облигаций *", key="manual_add_quantity")
                    coupon_type = x.selectbox("Тип купона выпуска", list(COUPON_TYPES), format_func=COUPON_TYPES.get, key="manual_add_coupon_type")
                    coupon_rate_text = y.text_input("Годовая купонная ставка, %", help="Используется только для фиксированного купона; для плавающего и неизвестного типа поле игнорируется.", key="manual_add_coupon_rate")
                    frequency = z.selectbox("Купонных выплат в год", [1, 2, 4, 12], key="manual_add_frequency")
                    coupon_dates_text = st.text_area("Ближайшие известные купонные даты · ГГГГ-ММ-ДД", help="По одной дате на строку. Для регулярного графика достаточно одной достоверной опорной даты.", key="manual_add_coupon_dates")
                    convention = st.selectbox("Конвенция начисления", DAY_COUNTS[:-1], key="manual_add_convention")
                    schedule_choice = st.radio("Тип графика", ["regular", "manual"], horizontal=True,
                        format_func=lambda value: "Регулярный график" if value == "regular" else "Полный график вручную", key="manual_add_schedule")
                prepare = st.form_submit_button("Проверить и показать карточку", type="primary")
            if prepare:
                st.session_state.manual_add_values = {
                    key: st.session_state.get(key) for key in _MANUAL_ADD_WIDGET_KEYS
                    if key in st.session_state
                }
                try:
                    maturity = parse_date(maturity_text)
                    if maturity is None:
                        raise ValueError("Укажите дату погашения в формате ГГГГ-ММ-ДД.")
                    if not name.strip():
                        raise ValueError("Укажите название облигации.")
                    face_value = _optional_number(face_text, "Номинал")
                    if face_value is None or face_value <= 0:
                        raise ValueError("Введите положительный номинал.")
                    try:
                        quantity = int(quantity_text.strip())
                    except ValueError as exc:
                        raise ValueError("Введите количество облигаций: положительное целое число.") from exc
                    if quantity <= 0:
                        raise ValueError("Количество облигаций должно быть положительным.")
                    coupon_rate = _optional_number(coupon_rate_text, "Купонная ставка") if coupon_type == "fixed" else None
                    known_dates = _date_list(coupon_dates_text)
                    candidate = Bond(name.strip(),
                        currency=currency.strip().upper() or "KZT", face_value=face_value,
                        quantity=quantity, coupon_rate_pct=coupon_rate,
                        valuation_date=valuation, maturity_date=maturity, coupon_frequency=frequency,
                        day_count=convention, user_supplied_coupon_dates=known_dates,
                        schedule_kind=schedule_choice, coupon_type=coupon_type, included=False)
                    errors = validate_portfolio([*st.session_state.bonds, candidate]).errors
                    if errors:
                        raise ValueError("; ".join(errors))
                    if candidate.valuation_date >= candidate.maturity_date:
                        raise ValueError("Дата погашения должна быть позже даты оценки.")
                    st.session_state.manual_add_draft = candidate.to_dict()
                    st.rerun()
                except (ValueError, TypeError, OverflowError) as exc:
                    st.error(str(exc))
    bonds = st.session_state.bonds
    if not bonds:
        empty("Портфель пуст", "Добавьте облигацию или импортируйте сохранённый портфель.")
        return
    a, d = st.columns([3, 1])
    search = a.text_input("Поиск по названию", placeholder="Введите название")
    sort = d.selectbox("Сортировка", ["Название", "Погашение", "YTM"])
    with st.expander("Фильтр по сроку"):
        maturity_filter = st.selectbox("Остаточный срок", ["Любой", "Не задан", "До года", "1–3 года", "Свыше 3 лет"])
    def matches(b):
        days = (b.maturity_date - (b.valuation_date or st.session_state.assessment_date)).days if b.maturity_date else None
        return (maturity_filter == "Любой" or (maturity_filter == "Не задан" and days is None) or
            (days is not None and ((maturity_filter == "До года" and 0 < days <= 365) or
            (maturity_filter == "1–3 года" and 365 < days <= 1095) or (maturity_filter == "Свыше 3 лет" and days > 1095))))
    filtered = [b for b in bonds if (not search or search.casefold() in b.name.casefold()) and matches(b)]
    if not filtered:
        empty("Нет позиций по выбранным фильтрам", "Измените поиск или фильтры.")
        return
    filtered.sort(key=lambda b: b.name.casefold() if sort == "Название" else b.maturity_date or date.max if sort == "Погашение" else b.ytm_pct if b.ytm_pct is not None else math.inf)
    all_yield_mode = all(st.session_state.get(f"bond_calculation_mode_{b.record_id}", "ytm") == "ytm" for b in filtered)
    min_table_ytm = min(-100.0 * (b.coupon_frequency if isinstance(b.coupon_frequency, int) and b.coupon_frequency > 0 else 1) + 1e-6 for b in filtered)
    frame = portfolio_table_frame(filtered)
    with st.form("compact_editor"):
        edited = st.data_editor(frame.set_index("record_id"), hide_index=True, width="stretch", placeholder="—", key=f"compact_{st.session_state.revision}",
            disabled=["position_label", "status"] + ([] if all_yield_mode else ["ytm_pct"]), column_config={
                "position_label": st.column_config.TextColumn("Позиция", disabled=True),
                # In Streamlit's grid, step also truncates cell data to that
                # precision before formatting. Keep it unset: %.4f rounds the
                # display while the editor retains the canonical float.
                "ytm_pct": st.column_config.NumberColumn("YTM, %", format="%.4f", min_value=min_table_ytm, max_value=1000.0),
                "coupon_rate_pct": st.column_config.NumberColumn("Купон, %", format="%.4f", min_value=0),
                "maturity_date": st.column_config.DateColumn("Погашение", format="DD.MM.YYYY"),
                "quantity": st.column_config.NumberColumn("Количество", min_value=1, step=1),
                "status": st.column_config.TextColumn("Статус", disabled=True)})
        save = st.form_submit_button("Сохранить таблицу")
    if not all_yield_mode:
        st.caption("YTM в этом списке отображается как рассчитанное значение; для ручного ввода переключите позицию на «Знаю YTM» в её карточке.")
    if save:
        try:
            updated_bonds = apply_portfolio_table_edits(bonds, edited)
            put_bonds(updated_bonds)
            st.rerun()
        except (ValueError, TypeError, OverflowError) as exc:
            st.error(str(exc))
    selected = st.selectbox("Карточка инструмента", [b.record_id for b in filtered], format_func=lambda key: next(bond_record_label(b) for b in filtered if b.record_id == key))
    bond = next(b for b in bonds if b.record_id == selected)
    with st.expander("Действия с позицией"):
        a, b = st.columns(2)
        if a.button("Дублировать", key=f"duplicate_{bond.record_id}"):
            candidate = Bond.from_dict({**bond.to_dict(), "record_id": None, "is_model": False,
                "isin": "", "name": bond.name + " · копия", "included": False,
                "provenance": {}, "schedule_status": "user", "schedule_confirmed": False})
            errors = validate_portfolio([*bonds, candidate]).errors
            if errors:
                st.error("; ".join(errors))
            else:
                put_bonds([*bonds, candidate])
                st.rerun()
        confirm = b.checkbox("Подтверждаю удаление позиции", key=f"remove_confirm_{bond.record_id}")
        if b.button("Удалить позицию", disabled=not confirm):
            put_bonds([x for x in bonds if x.record_id != bond.record_id])
            st.rerun()
    bond_card(bond)


def analysis_page():
    st.title("Анализ облигаций")
    bonds = st.session_state.bonds
    if not bonds:
        empty("Нет инструментов для анализа")
        return
    key = st.selectbox("Инструмент", [b.record_id for b in bonds], format_func=lambda key: next(bond_record_label(b) for b in bonds if b.record_id == key))
    bond_card(next(b for b in bonds if b.record_id == key))


def stress_table(rows):
    return [{"Сдвиг, б.п.": r["shock_bp"], "Линейный P&L, ₸": r["linear_change_kzt"], "Вклад выпуклости, ₸": r["convexity_contribution_kzt"],
        "P&L с выпуклостью, ₸": r["convexity_change_kzt"], "Точный P&L, ₸": r["exact_change_kzt"],
        "Точная стоимость, ₸": r["exact_value_kzt"], "Изменение, %": r["exact_change_pct"],
        "Ошибка приближения, ₸": r["approx_error_kzt"], "Статус": "Допустим" if r["valid"] else r["reason"],
        "Аналитические веса": r.get("weight_mode", "По рыночной стоимости"),
        "Аналитически · линейно, %": r.get("analytical_linear_change_pct"),
        "Аналитически · с выпуклостью, %": r.get("analytical_convexity_change_pct"),
        "Аналитически · точно, %": r.get("analytical_exact_change_pct"),
        "Основание оценки": r.get("valuation_note", "")} for r in rows]


PORTFOLIO_TABLE_FIELDS = ("ytm_pct", "coupon_rate_pct", "maturity_date", "quantity")


def portfolio_table_frame(bonds):
    """Build the table from canonical bond fields without display rounding."""
    return pd.DataFrame([{"record_id": b.record_id, "position_label": bond_record_label(b),
        "ytm_pct": b.ytm_pct if b.ytm_pct is not None else float("nan"),
        "coupon_rate_pct": b.coupon_rate_pct, "maturity_date": b.maturity_date, "quantity": b.quantity,
        "status": "Не включена в расчёт" if not b.included else calculation_readiness(b, mode())} for b in bonds])


def apply_portfolio_table_edits(bonds, edited):
    """Apply data_editor rows by record_id using model field names, never display labels."""
    by_id = {bond.record_id: copy.deepcopy(bond) for bond in bonds}
    for record_id, row in edited.iterrows():
        if record_id not in by_id:
            raise ValueError(f"Неизвестный record_id в таблице: {record_id}")
        original = by_id[record_id]
        payload = original.to_dict()
        for field in PORTFOLIO_TABLE_FIELDS:
            value = row[field]
            payload[field] = None if pd.isna(value) else value
        updated = Bond.from_dict(payload)
        if any(getattr(updated, field) != getattr(original, field)
               for field in ("coupon_rate_pct", "maturity_date")):
            updated.schedule_confirmed, updated.schedule_status = False, "user"
        by_id[record_id] = updated
    updated_bonds = list(by_id.values())
    errors = validate_portfolio(updated_bonds).errors
    if errors:
        raise ValueError("; ".join(errors))
    return updated_bonds


def stress_page():
    st.title("Стресс-тест")
    p = portfolio()
    if not p["complete"]:
        empty("Стресс-тест требует полного расчёта включённых позиций")
        issues(st.session_state.bonds)
        return
    st.caption("Параллельный сдвиг YTM · P&L по фактическим количествам. Это не автоматически рассчитанное последствие изменения базовой ставки Национального Банка.")
    if p.get("valuation_note"):
        st.info(p["valuation_note"])
    st.session_state.setdefault("stress_bp", 100)
    buttons = st.columns(7)
    def set_shock(value):
        st.session_state.stress_bp = value
    for col, bp in zip(buttons, [-200, -100, -50, 0, 50, 100, 200]):
        col.button(f"{bp:+} б.п.", on_click=set_shock, args=(bp,), width="stretch")
    shock = st.slider("Параллельный сдвиг, б.п.", -200, 200, step=10, key="stress_bp")
    included = [b for b in st.session_state.bonds if b.included]
    individual = st.checkbox("Индивидуальные сдвиги")
    per_bond = None
    if individual:
        edited = st.data_editor(pd.DataFrame([{"Позиция": bond_record_label(b), "Идентификатор": b.record_id, "Сдвиг, б.п.": shock} for b in included]),
            hide_index=True, width="stretch", placeholder="—", disabled=["Позиция", "Идентификатор"], key=f"individual_{st.session_state.revision}",
            column_config={"Сдвиг, б.п.": st.column_config.NumberColumn(min_value=-200, max_value=200, step=10)})
        per_bond = {f"RECORD:{row['Идентификатор']}": float(row["Сдвиг, б.п."]) for _, row in edited.iterrows()}
    try:
        r = stress_scenarios(included, [shock], per_bond_shocks=per_bond, calculation_mode=mode(), weight_mode=st.session_state.weight_mode)[0]
        kpis([("Базовая стоимость", compact_money(p["market_value"]), "Dirty · фактические позиции"),
              ("Линейный P&L", compact_money(r["linear_change_kzt"]), "Дюрация"),
              ("Вклад выпуклости", compact_money(r["convexity_contribution_kzt"]), "Поправка второго порядка"),
              ("Точная стоимость", compact_money(r["exact_value_kzt"]), "Повторное дисконтирование"),
              ("Точный P&L", compact_money(r["exact_change_kzt"]), f"{num(r['exact_change_pct'])}%")])
        if not r["valid"]:
            st.error(r["reason"])
        st.subheader("Аналитические веса и фактические позиции")
        st.caption(weight_basis_text(st.session_state.weight_mode))
        st.dataframe(pd.DataFrame([
            {"Основа": st.session_state.weight_mode, "Линейно, %": r["analytical_linear_change_pct"],
             "С выпуклостью, %": r["analytical_convexity_change_pct"], "Точно, %": r["analytical_exact_change_pct"]},
            {"Основа": "Фактические количества и Dirty price", "Линейно, %": r["linear_change_pct"],
             "С выпуклостью, %": r["convexity_change_pct"], "Точно, %": r["exact_change_pct"]},
        ]).style.format(na_rep="—", precision=4), hide_index=True, width="stretch", placeholder="—")
        st.caption("Аналитический процент не умножается на стоимость фактического портфеля. Денежный P&L — сумма переоценок реальных количеств.")
        st.subheader("Чувствительность к параллельному сдвигу")
        st.caption("График: изменение стоимости фактических позиций, %.")
        sensitivity_chart(included, "stress_curve")
        st.subheader("Выбранный сценарий · позиции")
        if r["positions"]:
            position_table = pd.DataFrame(r["positions"]).rename(columns={"name": "Инструмент", "shock_bp": "Шок, б.п.", "old_ytm_pct": "YTM до, %", "new_ytm_pct": "YTM после, %", "old_dirty_price": "Цена до, ₸", "new_dirty_price": "Цена после, ₸", "base_value_kzt": "Стоимость до, ₸", "new_value_kzt": "Стоимость после, ₸", "linear_pnl_kzt": "Линейный P&L, ₸", "convexity_contribution_kzt": "Вклад C, ₸", "approx_pnl_kzt": "P&L 2-го порядка, ₸", "exact_pnl_kzt": "Точный P&L, ₸", "approx_error_kzt": "Ошибка, ₸"})
            st.dataframe(position_table, hide_index=True, width="stretch", placeholder="—")
        with st.expander("Стандартные параллельные шоки"):
            rows = stress_table(stress_scenarios(included, [-200, -100, -50, 0, 50, 100, 200], calculation_mode=mode(), weight_mode=st.session_state.weight_mode))
            st.dataframe(pd.DataFrame(rows).style.format(na_rep="—", precision=4), hide_index=True, width="stretch", placeholder="—")
        with st.form("save_scenario"):
            name = st.text_input("Название сценария", "Рост доходностей")
            if st.form_submit_button("Сохранить сценарий", disabled=not r["valid"]):
                st.session_state.scenarios.append({"name": name.strip() or "Сценарий", "shock_bp": shock,
                    "individual_shocks": per_bond, "scenario_type": r["scenario_type"], "saved_mode": mode(),
                    "saved_on": date.today().isoformat(), "saved_result": r,
                    "portfolio_snapshot": [b.to_dict() for b in included]})
                st.success("Сценарий и снимок исходных данных сохранены.")
        st.download_button("Текущий сценарий · CSV", results_csv_bytes(stress_table([r])), "stress.csv", "text/csv")
    except (ValueError, TypeError) as exc:
        st.error(str(exc))


def scenarios_page():
    st.title("Сценарии")
    scenarios = st.session_state.scenarios
    if not scenarios:
        empty("Сохранённых сценариев нет", "Задайте шок на странице «Стресс-тест» и сохраните сценарий.")
        return
    st.caption("Пересчёт по текущему портфелю. Сохранённые снимки доступны отдельно.")
    rows = []
    for scenario in scenarios:
        try:
            r = stress_scenarios(st.session_state.bonds, [scenario["shock_bp"]], per_bond_shocks=scenario.get("individual_shocks"), calculation_mode=mode(), weight_mode=st.session_state.weight_mode)[0]
            rows.append({"Сценарий": scenario["name"], "Тип": scenario.get("scenario_type", "Параллельный"), **stress_table([r])[0]})
        except (ValueError, TypeError, KeyError) as exc:
            rows.append({"Сценарий": scenario["name"], "Статус": str(exc)})
    frame = pd.DataFrame(rows)
    st.dataframe(frame.style.format(na_rep="—", precision=4), hide_index=True, width="stretch", placeholder="—")
    if "Точный P&L, ₸" in frame:
        fig = go.Figure()
        for field, color in [("Линейный P&L, ₸", "#9CB5A8"), ("P&L с выпуклостью, ₸", "#E5C76B"), ("Точный P&L, ₸", "#65CFB2")]:
            fig.add_trace(go.Bar(x=frame["Сценарий"], y=frame[field], name=field, marker_color=color))
        chart_style(fig, "P&L, ₸")
        fig.update_layout(xaxis_title="Сценарий", barmode="group")
        st.plotly_chart(fig, width="stretch")
    st.download_button("Сравнение · CSV", results_csv_bytes(rows), "scenarios.csv", "text/csv")
    with st.expander("Сохранённые снимки"):
        st.json(snapshot_for_display(scenarios), expanded=False)
    confirm = st.checkbox("Подтверждаю удаление сценариев")
    if st.button("Удалить сценарии", disabled=not confirm):
        st.session_state.scenarios = []
        st.rerun()


def methodology_page():
    st.title("Методология")
    tabs = st.tabs(["Цена и НКД", "Риск и шоки", "Веса", "Данные и ограничения"])
    with tabs[0]:
        st.subheader("Доходность и теоретическая цена")
        st.write("YTM — годовая доходность к погашению в периодической модели. В интерфейсе вводится в процентах; y = YTM / 100, m — частота капитализации. Время tᵢ = фактические дни до выплаты / 365. Потоки на дату оценки считаются уже выплаченными.")
        st.latex(r"P_{dirty}=\sum_{d_i>d_0}\frac{CF_i}{(1+y/m)^{mt_i}},\quad 1+y/m>0")
        st.latex(r"P_{clean}=P_{dirty}-AI,\qquad V=P_{dirty}\,q")
        st.write("НКД (AI) — накопленный купон с начала текущего периода. Он уже содержится в dirty price и отдельно к ней не прибавляется. На купонную дату НКД равен нулю. Нужны обе границы периода либо дата начала начисления для первого купона.")
        st.latex(r"AI_{Act/365F}=N c\frac{d}{365},\quad AI_{30/360}=N c\frac{d_{30/360}}{360}")
        st.latex(r"AI_{coupon\ ratio}=C_{next}\frac{d_{actual}}{D_{actual}}")
        st.write("Поддержаны 30/360 US (NASD) и 30E/360 с разными правилами концов месяцев. Actual/Actual здесь — пропорция суммы фактического купона по дням периода; для регулярного купона C = Nc/m. Для нестандартного периода требуется явное правило начисления. Универсальный Actual/Actual ICMA для длинных периодов через квазипериоды не реализован.")
        st.subheader("Явный выбор методики цены")
        st.write("В карточке каждой облигации выбирается ACT/365F (стандартная универсальная модель) или KASE 30/360 по пунктам 11–14 и 18. Метод KASE доступен только вместе с конвенцией 30/360 KASE. Для регулярных годовых купонов mᵢ=1; общий вид поддерживаемой формулы:")
        st.latex(r"P_{dirty}^{KASE}=\sum_i\frac{CF_i}{(1+y/m_i)^{m_iF_i}},\quad m_i=\frac{360}{T_i},\quad F_i=\frac{T_{ki}}{360}")
        st.write("Tᵢ — длина купонного периода; Tₖᵢ — расчётные дни от даты оценки до потока по 30/360 KASE. Метод цены задаётся отдельно от конвенции НКД; старые записи без сохранённого метода остаются на ACT/365F.")
    with tabs[1]:
        st.subheader("Дюрация и выпуклость")
        st.latex(r"D_{Mac}=\frac{\sum_i t_iPV_i}{P},\qquad D_{mod}=\frac{D_{Mac}}{1+y/m}=-\frac{1}{P}\frac{\partial P}{\partial y}")
        st.latex(r"C=\frac{1}{P}\sum_i\frac{CF_i\,t_i(t_i+1/m)}{(1+y/m)^{mt_i+2}}=\frac{1}{P}\frac{\partial^2P}{\partial y^2}")
        st.latex(r"\frac{\Delta P}{P}\approx-D_{mod}\Delta y+\frac12 C(\Delta y)^2,\quad\Delta y=\frac{bp}{10000}")
        st.write("100 б.п. = 1 процентный пункт = 0,01 в десятичной доходности. D измеряется в годах, C — в год². Первая и вторая производные проверяются центральными конечными разностями с шагом 0,0001 по y, уменьшенным у границы модели.")
        st.latex(r"D_{mod}^{KASE}=\frac{1}{P}\sum_i\frac{PV_iF_i}{1+y/m_i},\quad C^{KASE}=\frac{1}{P}\sum_i\frac{PV_iF_i(F_i+1/m_i)}{(1+y/m_i)^2}")
        st.write("В методе KASE D_mac взвешивает сроки Fᵢ по базе 30/360; модифицированная дюрация и выпуклость получаются производными именно формулы KASE, а не переносом показателей ACT/365F.")
        st.write("Теоретическая переоценка повторно дисконтирует те же потоки с новой YTM; линейная и квадратичная оценки — приближения. Ошибка = приближённый P&L − точный P&L. Сдвиг YTM не равен прогнозу изменения базовой ставки.")
    with tabs[2]:
        st.subheader("Веса аналитики и денежный результат")
        st.latex(r"w_i^{equal}=1/n,\quad w_i^{nominal}=\frac{N_iq_i}{\sum_jN_jq_j},\quad w_i^{value}=\frac{P_iq_i}{\sum_jP_jq_j}")
        st.latex(r"D_p=\sum_iw_iD_i,\quad C_p=\sum_iw_iC_i,\quad\Delta V=\sum_iq_i(P_i^{shock}-P_i)")
        st.write("Пять включённых позиций при равных весах получают по 20%. После исключения позиции веса нормализуются среди оставшихся. Средние показатели следуют выбранным весам. Денежный P&L использует фактические количества и цены позиций; выбранную среднюю дюрацию не умножаем на общую стоимость. Неполная включённая позиция блокирует итог, а не исчезает из расчёта.")
    with tabs[3]:
        st.write("Приложение использует введённые значения как входные данные для выбранной даты оценки. Цена или доходность не являются заверением об официальной котировке.")
        st.write("Генератор использует месячную сетку от опорной даты, сохраняя конец месяца. Регулярный купон = номинал × ставка / частота. Для короткого/длинного первого и короткого последнего периода задаются фактические суммы; неизвестные суммы не достраиваются. Перенос на рабочие дни и биржевой календарь не реализованы.")
        st.write("Для регулярного фиксированного купона доступен генератор графика. Для плавающего, неизвестного или нестандартного купона вводите явные будущие выплаты; приложение не придумывает будущие суммы. НКД рассчитывается при наличии границ текущего периода, суммы купона и конвенции. Амортизация, оферты, дефолт, налоги, ex-coupon и валютная конвертация не моделируются. Смешанные даты оценки и валюты блокируют итог.")
        st.markdown("Методологические материалы: [FINRA о дюрации и выпуклости](https://syndication.finra.org/content/duration-what-interest-rate-hike-could-do-your-bond-portfolio), [QuantLib: тесты day-count и нестандартных периодов](https://github.com/lballabio/QuantLib/blob/master/test-suite/daycounters.cpp).")
        st.caption("Теоретическая оценка по введённым данным.")


def settings():
    return dict(weight_mode=st.session_state.weight_mode, scenarios=st.session_state.scenarios, calculation_mode=mode())


def data_page():
    st.title("Данные и экспорт")
    st.caption("В этой интернет-версии данные обрабатываются на сервере в текущей сессии. Подключение к биржевым котировкам отсутствует.")
    st.info("Не загружайте персональные или конфиденциальные сведения. Для сохранения результата на своём устройстве скачайте CSV, Excel или JSON. Постоянное хранение портфеля не предусмотрено; после завершения сессии данные могут быть потеряны.")
    import_tab, export_tab, manage_tab = st.tabs(["Открыть", "Сохранить и экспортировать", "Управление"])
    with import_tab:
        st.caption("Импорт поддерживает CSV, Excel и JSON. При добавлении текущие позиции сохраняются.")
        uploaded = st.file_uploader("Портфель · CSV / XLSX / JSON", type=["csv", "xlsx", "json"])
        action = st.radio("Импорт", ["Заменить портфель", "Добавить позиции"], horizontal=True)
        confirm = st.checkbox("Подтверждаю замену текущего портфеля", disabled=not st.session_state.bonds or action != "Заменить портфель")
        if st.button("Открыть файл", type="primary", disabled=not uploaded or (bool(st.session_state.bonds) and action == "Заменить портфель" and not confirm)):
            try:
                loaded = import_portfolio_bytes(uploaded.name, uploaded.getvalue())
                updated = loaded.bonds if action == "Заменить портфель" else [*st.session_state.bonds, *loaded.bonds]
                # Preserve incomplete/invalid drafts; duplicate identities must be fixed before merging.
                identifiers = [b.position_key for b in updated]
                ids = [b.record_id for b in updated]
                if len(identifiers) != len(set(identifiers)) or len(ids) != len(set(ids)):
                    raise ValueError("Повторяются идентификаторы позиций. Исправьте файл или замените портфель.")
                put_bonds(updated)
                if action == "Заменить портфель" and uploaded.name.lower().endswith(".json"):
                    st.session_state.pending_restore = {key: getattr(loaded, key) for key in ("weight_mode", "scenarios", "calculation_mode", "assessment_date")}
                st.session_state.import_notice = f"Загружено позиций: {len(loaded.bonds)}. Настройки JSON восстановлены при замене."
                st.rerun()
            except (ValueError, TypeError, OSError) as exc:
                st.error(str(exc))
        if st.session_state.get("import_notice"):
            st.success(st.session_state.pop("import_notice"))
        with st.expander("Формат файлов"):
            st.write("CSV: UTF-8 BOM, разделитель ;. Для импорта обязательна только колонка «Название»; старые файлы с дополнительными полями поддерживаются. Старые файлы JSON версии 1 и 2 читаются, включая денежные потоки, сохранённые портфели и настройки.")
    with export_tab:
        bonds = st.session_state.bonds
        a, b, c = st.columns(3)
        a.download_button("Портфель · CSV", csv_bytes(bonds), "kase_portfolio.csv", "text/csv")
        b.download_button("Портфель · Excel", excel_bytes(bonds), "kase_portfolio.xlsx")
        c.download_button("Полное состояние · JSON", portfolio_json_bytes(bonds, st.session_state.assessment_date, **settings()), "kase_portfolio.json", "application/json")
        st.caption("Кнопки выше скачивают файлы на ваше устройство. Произвольная запись на диск сервера в публичной версии отключена.")
        p = portfolio()
        if p["complete"]:
            rows = [{"name": b.name, "is_model": b.is_model, "record_id": b.record_id,
                "valuation_date": b.valuation_date.isoformat(), "ytm_pct": b.ytm_pct,
                "position_key": b.position_key, "mode": mode(), "weight_mode": st.session_state.weight_mode,
                "weight": p["weights"][b.position_key],
                **{k: v for k, v in p["analytics"][b.position_key].items() if k != "numerical"}} for b in bonds if b.included]
            stress = stress_table(stress_scenarios(bonds, [-200, -100, -50, 0, 50, 100, 200], calculation_mode=mode(), weight_mode=st.session_state.weight_mode))
            a, b = st.columns(2)
            a.download_button("Дюрация и выпуклость · CSV", results_csv_bytes(rows), "risk.csv", "text/csv")
            b.download_button("Дюрация и выпуклость · Excel", results_excel_bytes(rows), "risk.xlsx")
            a.download_button("Стресс-тест · CSV", results_csv_bytes(stress), "stress.csv", "text/csv")
            b.download_button("Стресс-тест · Excel", results_excel_bytes(stress), "stress.xlsx")
        else:
            st.caption("Экспорт результатов появится после подготовки включённых позиций.")
    with manage_tab:
        issues(st.session_state.bonds)
        confirm = st.checkbox("Подтверждаю очистку портфеля и сценариев")
        if st.button("Очистить текущую сессию", disabled=not confirm):
            put_bonds([])
            st.session_state.scenarios = []
            st.rerun()


def main():
    init_state()
    sidebar()
    {"Обзор": dashboard, "Портфель": portfolio_page, "Анализ облигаций": analysis_page,
     "Стресс-тест": stress_page, "Сценарии": scenarios_page, "Методология": methodology_page,
     "Данные и экспорт": data_page}[st.session_state.page]()


if __name__ == "__main__":
    main()
