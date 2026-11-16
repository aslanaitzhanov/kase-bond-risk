"""Калькулятор цены и процентного риска облигаций."""
from __future__ import annotations

from base64 import b64encode
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
                          results_excel_bytes, starter_bonds,
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
from ui_state import initialize_demo, load_demo, reset_portfolio, preserve_record_widgets
from scenario_risk import assess_scenario, position_risk_rows

MAIN_PAGES = ["Обзор", "Портфель", "Анализ облигаций", "Стресс-тест"]
PAGES = [*MAIN_PAGES, "Данные", "Методология", "Как пользоваться"]
PAGE_ALIASES = {"Данные и экспорт": "Данные", "Справка": "Методология", "Сценарии": "Стресс-тест"}
CHANGE_COLORS = {"negative": "#B42318", "positive": "#067647"}
BOND_ISSUER_LABELS = {
    "mum180_0009": "Минфин РК",
    "mtc048_0002": "Минфин РК",
    "mtc084_0001": "Минфин РК",
    "hsbkb22": "Халык Банк",
    "skkzb11": "Самрук-Қазына",
}
SENSITIVITY_SHOCKS_BP = range(-200, 201, 10)
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
app_dir = Path(__file__).resolve().parent
styles = (app_dir / "styles.css").read_text(encoding="utf-8")
aurora_background = b64encode((app_dir / "assets" / "aurora-background.svg").read_bytes()).decode("ascii")
st.markdown(
    f'<style>:root {{ --aurora-background: url("data:image/svg+xml;base64,{aurora_background}"); }}\n'
    f"{styles}</style>",
    unsafe_allow_html=True,
)


def init_state():
    initialize_demo(st.session_state)
    replacement = st.session_state.pop("pending_portfolio", None)
    if replacement is not None:
        reset_portfolio(st.session_state, replacement)
    notice = st.session_state.pop("pending_import_notice", None)
    if notice:
        st.session_state.import_notice = notice
    defaults = {"bonds": [], "scenarios": [], "revision": 0, "weight_mode": "Равные веса",
                "calculation_mode": "model", "assessment_date": date.today(), "page": "Обзор"}
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    st.session_state.page = PAGE_ALIASES.get(st.session_state.page, st.session_state.page)
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
    preserve_record_widgets(st.session_state)
    st.session_state.setdefault("active_shock_bp", 100)
    for key in ("risk_limit_pct", "individual_shifts", "position_risk_sort", "weight_mode", "assessment_date"):
        if key in st.session_state:
            st.session_state[key] = st.session_state[key]


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
    st.session_state.pop("active_individual_shocks", None)
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


def change_display(value, digits, unit=""):
    """Format and classify the displayed change without altering its raw value."""
    if value is None or not math.isfinite(float(value)):
        return "Н/Д", ""
    magnitude = num(abs(value), digits)
    if round(float(value), digits) == 0:
        return f"{magnitude}{unit}", ""
    prefix, tone = ("−", "negative") if value < 0 else ("+", "positive")
    return f"{prefix}{magnitude}{unit}", tone


def change_hover(value, unit):
    label, tone = change_display(value, 4, unit)
    color = CHANGE_COLORS.get(tone, "#172033")
    return f'<span style="color:{color}">{label}</span>'


def styled_changes(frame, fields):
    columns = frame.columns.intersection(fields)
    styler = frame.style.format(na_rep="—", precision=4)
    if not columns.empty:
        def cell_color(value):
            _, tone = change_display(value, 4)
            return f"color: {CHANGE_COLORS[tone]}" if tone else ""

        styler.format(lambda value: change_display(value, 4)[0], subset=columns)
        styler.map(cell_color, subset=columns)
    return styler


def uses_teaching_data(bonds):
    demo_ids = st.session_state.get("demo_record_ids", [])
    return any(b.is_model or b.record_id in demo_ids for b in bonds)


def kpis(items):
    cards = "".join(f'<div class="kpi"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>' for label, value in items)
    st.markdown(f'<div class="kpi-grid">{cards}</div>', unsafe_allow_html=True)


def go_page(name):
    st.session_state.page = PAGE_ALIASES.get(name, name)


def open_analysis(record_id):
    st.session_state.selected_record_id = record_id
    st.session_state.analysis_record = record_id
    go_page("Анализ облигаций")


def select_record(bonds, label, key):
    ids = [b.record_id for b in bonds]
    labels = {b.record_id: bond_record_label(b) for b in bonds}
    selected = st.session_state.get("selected_record_id")
    if selected not in ids:
        selected = ids[0]
    st.session_state[key] = selected
    def remember():
        st.session_state.selected_record_id = st.session_state[key]
    chosen = st.selectbox(label, ids, key=key, on_change=remember,
        format_func=lambda record_id: labels[record_id])
    st.session_state.selected_record_id = chosen
    return next(b for b in bonds if b.record_id == chosen)


def scenario_note(bonds):
    note = "Сдвиг YTM рассчитан при фиксированных выплатах. Сценарий не прогнозирует рынок."
    if any(requires_coupon_scenario(b) for b in bonds):
        note += " Реакция купонов на TONIA не моделируется."
    st.caption(note)


def portfolio_controls():
    with st.popover("Действия с портфелем", width="stretch"):
        clear = st.checkbox("Подтверждаю очистку портфеля и сценариев", key="clear_confirm")
        st.button("Очистить портфель", disabled=not clear,
                  on_click=reset_portfolio, args=(st.session_state,))
        replace = st.checkbox("Подтверждаю замену портфеля демонстрационным", key="restore_confirm",
                              disabled=not st.session_state.bonds)
        def restore():
            try:
                reset_portfolio(st.session_state, load_demo(), demo=True)
            except ValueError as exc:
                st.session_state.demo_error = str(exc)
        st.button("Восстановить демопортфель", disabled=bool(st.session_state.bonds) and not replace,
                  on_click=restore)


def guide_page():
    st.title("Как пользоваться")
    st.caption("Начните с примера на 29.10.2026 или загрузите свой портфель.")
    steps = [
        ("Добавьте облигации", "Введите параметры вручную или импортируйте файл.", "Портфель", "Открыть портфель", None),
        ("Проверьте выплаты", "Уточните даты и суммы будущих купонов и погашения.", "Анализ облигаций", "Открыть выплаты", "Выплаты"),
        ("Оцените облигацию", "Введите YTM или цену одной бумаги.", "Анализ облигаций", "Открыть оценку", "Параметры"),
        ("Оцените риск портфеля", "Посмотрите дюрацию и выпуклость.", "Обзор", "Открыть обзор", None),
        ("Задайте сценарий", "Сдвиньте YTM и проверьте лимит снижения.", "Стресс-тест", "Открыть стресс-тест", None),
    ]

    def open_step(page, tab):
        if tab:
            st.session_state.analysis_tabs = tab
        go_page(page)

    with st.container(key="guide"):
        columns = st.columns(3, gap="medium") + st.columns(2, gap="medium")
        for index, (column, step) in enumerate(zip(columns, steps), 1):
            title, description, page, action, tab = step
            with column.container(border=True):
                st.markdown(f"### {index}. {title}")
                st.write(description)
                st.button(action, on_click=open_step, args=(page, tab))

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


def portfolio_errors(result):
    for error in result["errors"]:
        for bond in st.session_state.bonds:
            error = error.replace(f"{bond.name} (позиция {bond.record_id[:8]})", bond_record_label(bond))
        st.warning(error)


def chart_style(fig, ytitle="Изменение стоимости, %"):
    fig.update_layout(template="plotly_white", paper_bgcolor="#FFFFFF", plot_bgcolor="#F7F8FA",
        font=dict(family="Segoe UI, sans-serif", color="#172033", size=13), height=360,
        margin=dict(l=12, r=16, t=12, b=14), hovermode="x unified",
        legend=dict(orientation="h", y=-0.28, x=0),
        xaxis=dict(title="Сдвиг YTM, б.п.", gridcolor="#D7DCE4", zerolinecolor="#94A3B8", automargin=True),
        yaxis=dict(title=ytitle, gridcolor="#D7DCE4", zerolinecolor="#94A3B8", automargin=True))
    return fig


def sensitivity_chart(bonds, key):
    rows = stress_scenarios(bonds, SENSITIVITY_SHOCKS_BP, calculation_mode=mode(),
                           weight_mode=st.session_state.weight_mode)
    fig = go.Figure()
    for label, field, color, dash in [("Дюрация", "linear_change_pct", "#475569", "dot"),
        ("Дюрация и выпуклость", "convexity_change_pct", "#172033", "dash"),
        ("Полная переоценка", "exact_change_pct", "#4338CA", "solid")]:
        values = [row[field] for row in rows]
        fig.add_trace(go.Scatter(x=[r["shock_bp"] for r in rows], y=values, name=label,
                                mode="lines", line=dict(color=color, width=2, dash=dash),
                                customdata=[change_hover(value, "%") for value in values],
                                hovertemplate="%{customdata}<extra>%{fullData.name}</extra>"))
    st.plotly_chart(chart_style(fig), width="stretch", key=key, theme=None)


def method_error_chart(bond):
    single = copy.deepcopy(bond)
    single.included = True
    unavailable = "Недостаточно данных для сравнения методов. Проверьте YTM, параметры облигации и график выплат."
    try:
        rows = stress_scenarios([single], SENSITIVITY_SHOCKS_BP, calculation_mode=mode(),
                               weight_mode=st.session_state.weight_mode)
    except (ValueError, ArithmeticError):
        st.info(unavailable)
        return

    fig = go.Figure()
    for label, field, color, dash in [
        ("Дюрация", "linear_change_pct", "#475569", "dash"),
        ("Дюрация + выпуклость", "convexity_change_pct", "#2563EB", "solid"),
    ]:
        errors = []
        for row in rows:
            approx, exact = row[field], row["exact_change_pct"]
            error = approx - exact if approx is not None and exact is not None else None
            errors.append(error if error is not None and math.isfinite(error) else None)
        fig.add_trace(go.Scatter(x=[row["shock_bp"] for row in rows], y=errors, name=label,
                                mode="lines", connectgaps=False, line=dict(color=color, width=2, dash=dash),
                                hovertemplate="%{y:+.6~g} п.п.<extra>%{fullData.name}</extra>"))
    if not any(value is not None for trace in fig.data for value in trace.y):
        st.info(unavailable)
        return

    chart_style(fig, "Отклонение от полной переоценки, п.п.")
    fig.update_xaxes(range=[SENSITIVITY_SHOCKS_BP[0], SENSITIVITY_SHOCKS_BP[-1]],
                     unifiedhovertitle_text="Сдвиг YTM: %{x:+d} б.п.")
    fig.update_yaxes(rangemode="tozero", zeroline=False)
    fig.add_hline(y=0, line=dict(color="#94A3B8", width=1, dash="dot"),
                  annotation_text="Полная переоценка — 0 п.п.", annotation_position="top left",
                  annotation_font_color="#475569")
    st.subheader("Ошибка методов относительно полной переоценки")
    st.plotly_chart(fig, width="stretch", key=f"bond_method_error_{bond.record_id}", theme=None)
    st.caption("Чем ближе линия к нулю, тем точнее метод. Отрицательное отклонение означает, что метод занижает изменение цены, положительное — завышает")


def bond_display_name(name):
    issuer = BOND_ISSUER_LABELS.get(name.casefold())
    return f"{name} ({issuer})" if issuer else name


def bond_record_label(bond):
    label = bond_display_name(bond.name)
    same = [b.record_id for b in st.session_state.get("bonds", []) if b.name == bond.name]
    return f"{label} · позиция {same.index(bond.record_id) + 1}" if len(same) > 1 else label


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
    hidden = {"isin", "issuer", "record_id", "position_key", "data_source", "data_checked_date", "ytm_source", "ytm_origin",
              "ytm_observation_date", "provenance", "schedule_source", "schedule_status", "source", "status", "is_model"}
    if isinstance(value, dict):
        return {key: snapshot_for_display(item) for key, item in value.items() if key not in hidden and not key.startswith("RECORD:")}
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
        st.markdown('<div class="brand">KASE Bond Risk</div>', unsafe_allow_html=True)
        active = st.session_state.page
        st.button("Как пользоваться", key="nav_guide", on_click=go_page, args=("Как пользоваться",),
                  type="primary" if active == "Как пользоваться" else "secondary", width="stretch")
        st.session_state.main_navigation = active if active in MAIN_PAGES else None
        def navigate():
            go_page(st.session_state.main_navigation)
        st.radio("Разделы", MAIN_PAGES, key="main_navigation", index=None,
                 label_visibility="collapsed", on_change=navigate)
        st.divider()
        st.button("Данные", key="nav_data", on_click=go_page, args=("Данные",),
                  type="primary" if active == "Данные" else "secondary", width="stretch")
        st.button("Методология", key="nav_help", on_click=go_page, args=("Методология",),
                  type="primary" if active == "Методология" else "secondary", width="stretch")


def context_line():
    bonds = st.session_state.bonds
    dates = sorted({b.valuation_date for b in bonds if b.included and b.valuation_date})
    value = ", ".join(d.strftime("%d.%m.%Y") for d in dates)
    if not value:
        assessment = st.session_state.assessment_date
        value = assessment.strftime("%d.%m.%Y") if assessment else "Дата не задана"
    line = f"Дата оценки · {value}  /  Облигаций · {len(bonds)}"
    st.markdown(f'<div class="context">{html.escape(line)}</div>', unsafe_allow_html=True)


def portfolio_settings():
    with st.popover("Настройки портфеля", width="stretch"):
        st.selectbox("Веса аналитики", WEIGHT_MODES, key="weight_mode", help=weight_basis_text(st.session_state.weight_mode))
        assessment = st.date_input("Дата оценки", key="assessment_date", format="DD.MM.YYYY")
        if st.button("Применить дату ко всем", width="stretch"):
            if assessment is None:
                st.error("Укажите дату оценки.")
            else:
                updated = copy.deepcopy(st.session_state.bonds)
                for bond in updated:
                    bond.valuation_date = assessment
                put_bonds(updated)
                st.rerun()
        portfolio_weight_details()

def portfolio_weight_details():
    bonds, p = st.session_state.bonds, portfolio()
    included = [b for b in bonds if b.included]
    excluded = [b for b in bonds if not b.included]
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
                "Состав": "Включена" if b.included else "Исключена"} for b in bonds]).style.format(na_rep="—", precision=4), hide_index=True, width="stretch", placeholder="—")

def dashboard():
    st.title("Обзор")
    if st.session_state.get("demo_error"):
        st.error(st.session_state.demo_error)
    context_line()
    p = portfolio()
    risk = p.get("risk", {})
    kpis([("Модельная стоимость", compact_money(p.get("market_value"))),
          ("Модифицированная дюрация, лет", num(risk.get("modified_duration_years"))),
          ("Выпуклость, лет²", num(risk.get("convexity")))])
    if uses_teaching_data(b for b in st.session_state.bonds if b.included):
        st.caption("Учебные данные")
    with st.container(horizontal=True, key="overview_actions"):
        st.button("Открыть портфель", on_click=go_page, args=("Портфель",), type="primary")
        st.button("Провести стресс-тест", on_click=go_page, args=("Стресс-тест",))
    if not p["complete"]:
        portfolio_errors(p)
    with st.popover("Подробные показатели"):
        st.write("Полная стоимость: " + money(p.get("market_value")))
        st.write("Дюрация Маколея: " + num(risk.get("macaulay_duration_years")) + " года")
        st.write("Модифицированная дюрация: " + num(risk.get("modified_duration_years"), 6) + " года")
        st.write("Выпуклость: " + num(risk.get("convexity"), 6) + " года²")
        st.caption("Веса: " + st.session_state.weight_mode)
        included = [b for b in st.session_state.bonds if b.included]
        if included and len(included) != len(st.session_state.bonds):
            st.caption(f"В расчёте {len(included)} из {len(st.session_state.bonds)} позиций; веса нормализованы по включённым.")

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
    with st.expander("Как подготовить график", expanded=False):
        st.write("Укажите даты и суммы будущих выплат. Приложение проверит порядок дат, купонный график и погашение. "
                 "Регулярный фиксированный купон: номинал × годовая ставка ÷ число выплат. "
                 "Изменяемые ставки и нестандартные периоды требуют явных сумм.")
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
    with st.expander("Текущий купонный период", key="current_coupon_period", on_change="rerun"):
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


def pricing_method_panel(bond):
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


def price_inputs(bond):
    selected_mode = st.session_state.get(f"bond_calculation_mode_{bond.record_id}", "ytm")
    pricing_method_panel(bond)
    derived_ytm = None
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
            observed_price = st.number_input("Цена одной облигации", min_value=0.0001,
                value=stored_result.get("price"), step=0.0001, format="%.4f", placeholder="—",
                key=f"price_value_{bond.record_id}_{revision}")
            second, third = st.columns(2)
            price_type = second.selectbox("Тип цены", ["clean", "dirty"],
                index=1 if stored_result.get("price_type") == "dirty" else 0,
                format_func=lambda v: "Чистая · без НКД" if v == "clean" else "Полная · с НКД",
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
            derived_ytm = bond.ytm_pct
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
                    raise ValueError("Для чистой цены нужен НКД: " + accrued_note)
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


    return derived_ytm


def open_coupon_period():
    st.session_state.analysis_tabs = "Выплаты"
    st.session_state.current_coupon_period = True


def price_results(bond, derived_ytm):
    accrued, note = accrued_for_bond(bond)
    result = result_for(bond) or {}
    st.subheader("Результат оценки")
    with st.container(key="main_price"):
        st.metric("Полная цена", money(result.get("dirty_price")))
        if derived_ytm is not None:
            st.metric("Рассчитанная YTM", f"{num(derived_ytm, 4)}%")
    with st.container(key="price_breakdown"):
        b, c = st.columns(2)
        b.metric("Чистая цена", money(result.get("clean_price")), help="Clean price · без НКД")
        c.metric("НКД на облигацию", money(accrued), help=note)
    if uses_teaching_data([bond]):
        st.caption("Учебные данные")
    if not result and st.session_state[f"bond_calculation_mode_{bond.record_id}"] == "ytm":
        errors = validate_bond(bond, require_calculable=True, mode=mode()).errors
        if errors:
            st.warning(" ".join(errors))
    if accrued is None:
        st.caption("НКД: " + note.removeprefix("Недостаточно данных: "))
        st.button("Указать купонный период", on_click=open_coupon_period)


def accrual_details(bond):
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


def risk_panel(bond):
    r = result_for(bond)
    if not r:
        st.info("Недостаточно данных для оценки риска. Проверьте YTM, параметры облигации и график выплат.")
        return
    a, b, c = st.columns(3)
    a.metric("Маколей, лет", num(r["macaulay_duration_years"]))
    b.metric("Модифицированная дюрация, лет", num(r["modified_duration_years"]))
    c.metric("Выпуклость, год²", num(r["convexity"]))
    method_error_chart(bond)
    with st.expander("Проверка производных конечными разностями"):
        st.dataframe(pd.DataFrame([{"Метрика": label, "Аналитически": r[field], "Численно": r["numerical"][numeric], "Абс. разница": r[diff]}
            for label, field, numeric, diff in [("D_mod", "modified_duration_years", "numerical_modified_duration", "duration_abs_diff"),
                ("Выпуклость", "convexity", "numerical_convexity", "convexity_abs_diff")]]), hide_index=True, width="stretch", placeholder="—")


def bond_card(bonds):
    inputs, results = st.columns(2)
    with inputs.container(key="bond_inputs", border=True):
        st.subheader("Исходные данные")
        bond = select_record(bonds, "Облигация", "analysis_record")
        valuation = bond.valuation_date.strftime("%d.%m.%Y") if bond.valuation_date else "Н/Д"
        maturity = bond.maturity_date.strftime("%d.%m.%Y") if bond.maturity_date else "Н/Д"
        st.caption(f"Оценка {valuation} · Погашение {maturity} · Купон {COUPON_TYPES[bond.coupon_type].lower()}")
        if not bond.included:
            st.caption("Позиция исключена из портфельного расчёта")
        derived_ytm = bond_valuation(bond)
    with results.container(key="price_results", border=True):
        price_results(bond, derived_ytm)
    tabs = st.tabs(["Параметры", "Выплаты", "Риск"], key="analysis_tabs", on_change="rerun")
    with tabs[0]:
        parameters(bond)
    with tabs[1]:
        if requires_coupon_scenario(bond) or accrued_for_bond(bond)[0] is None or "current_coupon_period" in bond.provenance:
            current_period_panel(bond)
        schedule_editor(bond)
        accrued, note = accrued_for_bond(bond)
        with st.expander("Подробности НКД"):
            st.write("НКД позиции: " + money(accrued * bond.quantity if accrued is not None and bond.quantity else None))
            if accrued is not None:
                st.caption(note)
            st.caption("Конвенция: " + (bond.day_count or "не задана"))
            accrual_details(bond)
    with tabs[2]:
        risk_panel(bond)


def bond_valuation(bond):
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
        format_func=lambda v: "По YTM" if v == "ytm" else "По цене",
        on_change=switch_mode)
    return price_inputs(bond)


def close_add():
    st.session_state.manual_add_open = False


@st.dialog("Добавить облигацию", width="large", on_dismiss=close_add)
def add_bond_dialog():
    draft_data = st.session_state.get("manual_add_draft")
    if not draft_data:
        remembered = st.session_state.get("manual_add_values", {})
        for key, value in remembered.items():
            if key not in st.session_state:
                st.session_state[key] = value
    if draft_data:
        draft = Bond.from_dict(draft_data)
        label = bond_display_name(draft.name)
        st.info(f"Карточка облигации · {label}")
        st.dataframe(pd.DataFrame([{
            "Название": label, "Дата оценки": draft.valuation_date,
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
                st.session_state.added_record_id = draft.record_id
                st.session_state.selected_record_id = draft.record_id
                st.session_state.pop("manual_add_draft", None)
                for key in _MANUAL_ADD_WIDGET_KEYS:
                    st.session_state.pop(key, None)
                st.session_state.pop("manual_add_values", None)
                st.session_state.manual_add_open = False
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
            x, y, z = st.columns(3)
            face_text = x.text_input("Номинал одной облигации *", key="manual_add_face")
            quantity_text = y.text_input("Количество облигаций *", key="manual_add_quantity")
            coupon_type = z.selectbox("Тип купона выпуска", list(COUPON_TYPES), format_func=COUPON_TYPES.get, key="manual_add_coupon_type")
            coupon_rate_text = x.text_input("Годовая купонная ставка, %", key="manual_add_coupon_rate", help="Для фиксированного купона; иначе будущие суммы задаются явно.")
            frequency = y.selectbox("Купонных выплат в год", [1, 2, 4, 12], key="manual_add_frequency")
            with st.expander("Дополнительно"):
                currency = st.text_input("Валюта", max_chars=3, key="manual_add_currency").upper()
                coupon_dates_text = st.text_area("Ближайшие известные купонные даты · ГГГГ-ММ-ДД", help="По одной дате на строку.", key="manual_add_coupon_dates")
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

def portfolio_page():
    st.title("Портфель")
    with st.container(key="portfolio_toolbar"):
        a, b, c, d = st.columns([1.4, .7, 1.3, 1.4])
        def show_add():
            st.session_state.manual_add_open = True
        a.button("Добавить облигацию", key="open_add", on_click=show_add, type="primary", width="stretch")
        b.button("Импорт", on_click=go_page, args=("Данные",), width="stretch")
        with c:
            portfolio_settings()
        with d:
            portfolio_controls()
    if st.session_state.get("manual_add_open"):
        add_bond_dialog()
    bonds = st.session_state.bonds
    if not bonds:
        empty("Портфель пуст", "Добавьте облигацию или импортируйте сохранённый портфель.")
        return
    with st.container(key="portfolio_filters"):
        a, d = st.columns([4, 1])
        search = a.text_input("Поиск по названию", placeholder="Поиск облигации", label_visibility="collapsed")
        with d.popover("Фильтры", width="stretch"):
            sort = st.selectbox("Сортировка", ["Название", "Погашение", "YTM"])
            maturity_filter = st.selectbox("Остаточный срок", ["Любой", "Не задан", "До года", "1–3 года", "Свыше 3 лет"])
            extra = st.checkbox("Дополнительные столбцы")
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
            column_order=["position_label", "quantity", "ytm_pct", "coupon_rate_pct", "maturity_date", "position_value"] + (["status"] if frame["status"].any() else []) + (["dirty_price", "included"] if extra else []),
            disabled=["position_label", "status", "position_value", "dirty_price", "included"] + ([] if all_yield_mode else ["ytm_pct"]), column_config={
                "_index": None,
                "position_label": st.column_config.TextColumn("Название", disabled=True),
                # In Streamlit's grid, step also truncates cell data to that
                # precision before formatting. Keep it unset: %.4f rounds the
                # display while the editor retains the canonical float.
                "ytm_pct": st.column_config.NumberColumn("YTM, %", format="%.4f", min_value=min_table_ytm, max_value=1000.0),
                "coupon_rate_pct": st.column_config.NumberColumn("Купон, %", format="%.4f", min_value=0),
                "maturity_date": st.column_config.DateColumn("Погашение", format="DD.MM.YYYY"),
                "quantity": st.column_config.NumberColumn("Количество", min_value=1, step=1),
                "position_value": st.column_config.NumberColumn("Стоимость, ₸", format="%.2f", disabled=True),
                "dirty_price": st.column_config.NumberColumn("Полная цена, ₸", format="%.4f", disabled=True),
                "included": st.column_config.CheckboxColumn("Включена в расчёт", disabled=True),
                "status": st.column_config.TextColumn("Требует внимания", disabled=True)})
        save = st.form_submit_button("Сохранить таблицу")
    if save:
        try:
            updated_bonds = apply_portfolio_table_edits(bonds, edited)
            put_bonds(updated_bonds)
            st.rerun()
        except (ValueError, TypeError, OverflowError) as exc:
            st.error(str(exc))
    with st.container(key="portfolio_selection"):
        a, b, c = st.columns([2, 1, 1], vertical_alignment="bottom")
        with a:
            bond = select_record(filtered, "Позиция для анализа", "portfolio_record")
        b.button("Перейти к анализу", key="added_analysis", on_click=open_analysis, args=(bond.record_id,), width="stretch")
        with c:
            position_actions(bond)


def position_actions(bond):
    bonds = st.session_state.bonds
    with st.popover("Действия с позицией", width="stretch"):
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


def analysis_page():
    st.title("Анализ облигаций")
    bonds = st.session_state.bonds
    if not bonds:
        empty("Нет инструментов для анализа")
        return
    bond_card(bonds)

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


SCENARIO_CHANGE_FIELDS = (
    "Линейный P&L, ₸", "Вклад выпуклости, ₸", "P&L с выпуклостью, ₸", "Точный P&L, ₸",
    "Изменение, %", "Аналитически · линейно, %", "Аналитически · с выпуклостью, %", "Аналитически · точно, %",
)


PORTFOLIO_TABLE_FIELDS = ("ytm_pct", "coupon_rate_pct", "maturity_date", "quantity")


def portfolio_table_frame(bonds):
    """Build the editable view without rounding or copying saved record state."""
    rows = []
    for bond in bonds:
        result = result_for(bond) or {}
        status = "Не включена" if not bond.included else calculation_readiness(bond, mode())
        rows.append({"record_id": bond.record_id, "position_label": bond_record_label(bond),
            "ytm_pct": bond.ytm_pct if bond.ytm_pct is not None else float("nan"),
            "coupon_rate_pct": bond.coupon_rate_pct, "maturity_date": bond.maturity_date,
            "quantity": bond.quantity, "position_value": result.get("position_market_value"),
            "dirty_price": result.get("dirty_price"), "included": bond.included,
            "status": "" if status == "Готово к расчёту" else status})
    return pd.DataFrame(rows)


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


def scenario_impact(rows):
    pct = "Изменение цены, %"
    kzt = "Изменение стоимости, ₸"
    largest_move = max(rows, key=lambda row: abs(row[pct]))
    largest_money_loss = min(rows, key=lambda row: row[kzt])
    largest_price_loss = min(rows, key=lambda row: row[pct])
    highlights = [
        ("Наибольшая чувствительность", largest_move, pct, 4, "%", True),
        ("Наибольшее снижение в тенге", largest_money_loss, kzt, 2, " ₸", largest_money_loss[kzt] < 0),
        ("Наибольшее снижение в процентах", largest_price_loss, pct, 4, "%", largest_price_loss[pct] < 0),
    ]
    st.subheader("Что сильнее всего влияет на результат")
    cards = []
    for label, row, field, digits, unit, has_value in highlights:
        name = html.escape(row["Инструмент"]) if has_value else ""
        value, tone = change_display(row[field], digits, unit) if has_value else ("Нет снижения", "")
        cards.append(f'<div class="impact-card"><span>{label}</span>'
                     f'<div class="impact-name">{name}</div><strong class="{tone}">{value}</strong></div>')
    st.markdown(f'<div class="impact-grid">{"".join(cards)}</div>', unsafe_allow_html=True)


def position_details(rows):
    with st.expander("Вклад позиций"):
        order = st.selectbox("Сортировка позиций", ["Денежное снижение", "Процентная чувствительность"], key="position_risk_sort")
        field = "Изменение стоимости, ₸" if order == "Денежное снижение" else "Изменение цены, %"
        rows.sort(key=lambda row: row[field] if order == "Денежное снижение" else -abs(row[field]))
        changes = styled_changes(pd.DataFrame(rows), ("Изменение цены, %", "Изменение стоимости, ₸"))
        st.dataframe(changes, hide_index=True, width="stretch")


def stress_page():
    st.title("Стресс-тест")
    current, saved = st.tabs(["Сценарий", "Сохранённые сценарии"])
    with current:
        stress_current()
    with saved:
        scenarios_page()


def stress_current():
    p = portfolio()
    if not p["complete"]:
        empty("Стресс-тест требует полного расчёта включённых позиций")
        portfolio_errors(p)
        return
    st.session_state.stress_bp = st.session_state.active_shock_bp
    def remember_shock():
        st.session_state.active_shock_bp = st.session_state.stress_bp
    with st.container(key="stress_controls", border=True):
        a, b = st.columns([2, 1])
        shock = a.slider("Сдвиг YTM, б.п.", -200, 200, step=10, key="stress_bp", on_change=remember_shock,
                         help="100 б.п. = 1 процентный пункт YTM")
        limit = b.number_input("Предел снижения, %", min_value=0.0, value=3.0, step=0.1, key="risk_limit_pct",
            help="Сравнение выполняется до округления по фактической стоимости позиций.")
    included = [b for b in st.session_state.bonds if b.included]
    individual = st.checkbox("Индивидуальные сдвиги", key="individual_shifts")
    per_bond = None
    if individual:
        previous_shocks = st.session_state.get("active_individual_shocks") or {}
        edited = st.data_editor(pd.DataFrame([{"Позиция": bond_record_label(b), "Идентификатор": b.record_id,
            "Сдвиг, б.п.": previous_shocks.get(b.position_key, shock)} for b in included]),
            hide_index=True, width="stretch", placeholder="—", disabled=["Позиция", "Идентификатор"], key=f"individual_{st.session_state.revision}",
            column_config={"Идентификатор": None, "Сдвиг, б.п.": st.column_config.NumberColumn(min_value=-200, max_value=200, step=10)})
        per_bond = {f"RECORD:{row['Идентификатор']}": float(row["Сдвиг, б.п."]) for _, row in edited.iterrows()}
    st.session_state.active_individual_shocks = per_bond
    try:
        r = stress_scenarios(included, [shock], per_bond_shocks=per_bond, calculation_mode=mode(), weight_mode=st.session_state.weight_mode)[0]
        if not r["valid"]:
            st.error(r["reason"])
        assessment = assess_scenario(r, p.get("market_value"), limit, complete=p["complete"])
        amount, amount_tone = change_display(r["exact_change_kzt"], 2, " ₸")
        percent, percent_tone = change_display(assessment["change_pct"] if assessment else None, 4, "%")
        if assessment is None:
            status = '<strong>Лимит недоступен</strong><small>Нужна полная переоценка и положительная исходная стоимость.</small>'
        elif assessment["exceeded"]:
            status = f'<strong class="negative">Превышает выбранный лимит</strong><small>Превышение {num(-assessment["margin_pp"], 4)} п.п.</small>'
        else:
            status = f'<strong class="positive">В пределах выбранного лимита</strong><small>Запас {num(assessment["margin_pp"], 4)} п.п.</small>'
        with st.container(key="stress_result", border=True):
            st.subheader("Результат сценария")
            st.markdown(
                f'<div class="result"><div><span>Изменение стоимости</span>'
                f'<strong class="change {percent_tone}">{percent}</strong>'
                f'<strong class="amount {amount_tone}">{amount}</strong></div>'
                f'<div><span>Стоимость после сдвига</span><strong class="amount">{money(r["exact_value_kzt"])}</strong></div>'
                f'<div class="limit-status"><span>Лимит снижения · {num(limit, 2)}%</span>{status}</div></div>',
                unsafe_allow_html=True,
            )
            if amount_tone == "positive" or percent_tone == "positive":
                st.caption("Стоимость растёт, сценарное снижение 0%.")
            if uses_teaching_data(included):
                st.caption("Учебные данные")
            scenario_note(included)
        rows = position_risk_rows(r, p["analytics"])
        labels = {b.position_key: bond_record_label(b) for b in included}
        for row, position in zip(rows, r["positions"]):
            row["Инструмент"] = labels[position["position_key"]]
        if rows:
            scenario_impact(rows)
            position_details(rows)
        with st.expander("Сравнение методов", expanded=False):
            sensitivity_chart(included, "stress_curve")
        with st.container(horizontal=True, key="scenario_actions"):
            with st.popover("Сохранить сценарий"):
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
    st.dataframe(styled_changes(frame, SCENARIO_CHANGE_FIELDS), hide_index=True, width="stretch", placeholder="—")
    if "Точный P&L, ₸" in frame:
        fig = go.Figure()
        for field, pattern in [("Линейный P&L, ₸", "/"), ("P&L с выпуклостью, ₸", "."), ("Точный P&L, ₸", "")]:
            values = frame[field]
            colors = [CHANGE_COLORS.get(change_display(value, 4)[1], "#475569") for value in values]
            fig.add_trace(go.Bar(x=frame["Сценарий"], y=values, name=field,
                                marker=dict(color=colors, pattern=dict(shape=pattern)),
                                customdata=[change_hover(value, " ₸") for value in values],
                                hovertemplate="%{customdata}<extra>%{fullData.name}</extra>"))
        chart_style(fig, "P&L, ₸")
        fig.update_layout(xaxis_title="Сценарий", barmode="group")
        st.plotly_chart(fig, width="stretch", theme=None)
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
        st.subheader("Оценка сценарного лимита")
        st.latex(r"\Delta V_{\%}=100\frac{V_{scenario}-V_0}{V_0},\quad L=\max(0,-\Delta V_{\%}),\quad V_0>0")
        st.write("Проверка использует существующую полную переоценку фактических количеств. "
                 "Предел превышен только при L больше пользовательского лимита; равенство допустимо. "
                 "Сравнение выполняется до округления. Начальные 3% — изменяемый учебный пример, не норматив. "
                 "Запас равен лимиту минус сценарное снижение, в процентных пунктах. "
                 "Вероятности сценариев и доходность будущего периода владения не рассчитываются.")
    with tabs[3]:
        st.write("Приложение использует введённые значения как входные данные для выбранной даты оценки. Цена или доходность не являются заверением об официальной котировке.")
        st.write("Генератор использует месячную сетку от опорной даты, сохраняя конец месяца. Регулярный купон = номинал × ставка / частота. Для короткого/длинного первого и короткого последнего периода задаются фактические суммы; неизвестные суммы не достраиваются. Перенос на рабочие дни и биржевой календарь не реализованы.")
        st.write("Для регулярного фиксированного купона доступен генератор графика. Для плавающего, неизвестного или нестандартного купона вводите явные будущие выплаты; приложение не придумывает будущие суммы. НКД рассчитывается при наличии границ текущего периода, суммы купона и конвенции. Амортизация, оферты, дефолт, налоги, ex-coupon и валютная конвертация не моделируются. Смешанные даты оценки и валюты блокируют итог.")
        st.markdown("Методологические материалы: [FINRA о дюрации и выпуклости](https://syndication.finra.org/content/duration-what-interest-rate-hike-could-do-your-bond-portfolio), [QuantLib: тесты day-count и нестандартных периодов](https://github.com/lballabio/QuantLib/blob/master/test-suite/daycounters.cpp).")
        st.caption("Учебные данные включают демопортфель на 29.10.2026 с модельными YTM. Такие доходности не являются текущими котировками KASE.")


def settings():
    return dict(weight_mode=st.session_state.weight_mode, scenarios=st.session_state.scenarios, calculation_mode=mode())


def data_page():
    st.title("Данные")
    st.caption("В интернет-версии загруженные данные обрабатываются сервером приложения. Автоматического подключения к KASE нет.")
    import_tab, export_tab = st.tabs(["Импорт", "Экспорт"])
    with import_tab:
        st.caption("Импорт поддерживает CSV, Excel и JSON. При добавлении текущие позиции сохраняются.")
        uploaded = st.file_uploader("Портфель · CSV / XLSX / JSON", type=["csv", "xlsx", "json"])
        action = st.radio("Импорт", ["Заменить портфель", "Добавить позиции"], horizontal=True)
        confirm = st.checkbox("Подтверждаю замену текущего портфеля", disabled=not st.session_state.bonds or action != "Заменить портфель")
        if st.button("Открыть файл", type="primary", disabled=not uploaded or (bool(st.session_state.bonds) and action == "Заменить портфель" and not confirm)):
            st.session_state.pop("import_notice", None)
            try:
                loaded = import_portfolio_bytes(uploaded.name, uploaded.getvalue())
                updated = loaded.bonds if action == "Заменить портфель" else [*st.session_state.bonds, *loaded.bonds]
                # Preserve incomplete/invalid drafts; duplicate identities must be fixed before merging.
                identifiers = [b.position_key for b in updated]
                ids = [b.record_id for b in updated]
                if len(identifiers) != len(set(identifiers)) or len(ids) != len(set(ids)):
                    raise ValueError("Повторяются идентификаторы позиций. Исправьте файл или замените портфель.")
                if action == "Заменить портфель":
                    if not uploaded.name.lower().endswith(".json"):
                        loaded.weight_mode = st.session_state.weight_mode
                        loaded.assessment_date = loaded.assessment_date or st.session_state.assessment_date
                    st.session_state.pending_portfolio = loaded
                else:
                    put_bonds(updated)
                st.session_state.pending_import_notice = f"Загружено позиций: {len(loaded.bonds)} · {uploaded.name}"
                st.rerun()
            except (ValueError, TypeError, OSError) as exc:
                st.error(str(exc))
        if st.session_state.get("import_notice"):
            st.success(st.session_state.import_notice)
        with st.expander("Формат файлов"):
            st.write("CSV: UTF-8 BOM, разделитель ;. Для импорта обязательна только колонка «Название»; старые файлы с дополнительными полями поддерживаются. Старые файлы JSON версии 1 и 2 читаются, включая денежные потоки, сохранённые портфели и настройки.")
    with export_tab:
        bonds = st.session_state.bonds
        a, b, c = st.columns(3)
        a.download_button("Портфель · CSV", csv_bytes(bonds), "kase_portfolio.csv", "text/csv")
        b.download_button("Портфель · Excel", excel_bytes(bonds), "kase_portfolio.xlsx")
        c.download_button("Полное состояние · JSON", portfolio_json_bytes(bonds, st.session_state.assessment_date, **settings()), "kase_portfolio.json", "application/json")
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


def main():
    init_state()
    sidebar()
    {"Как пользоваться": guide_page, "Обзор": dashboard, "Портфель": portfolio_page,
     "Анализ облигаций": analysis_page, "Стресс-тест": stress_page,
     "Методология": methodology_page, "Данные": data_page}[st.session_state.page]()


if __name__ == "__main__":
    main()
