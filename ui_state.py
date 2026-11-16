"""Session-local UI lifecycle; the authored demo is read without modification."""
from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path

from calculations import portfolio_analytics
from data_manager import import_portfolio_bytes
from validation import validate_portfolio

DEMO_PATH = Path(__file__).with_name("demo_portfolio.json")
DEMO_DATE = date(2026, 10, 29)
DEMO_NAMES = {"mum180_0009", "mtc048_0002", "mtc084_0001", "hsbkb22", "skkzb11"}


def demo_fingerprint(bonds):
    return hashlib.sha256(json.dumps([b.to_dict() for b in bonds], sort_keys=True,
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def load_demo():
    try:
        loaded = import_portfolio_bytes(DEMO_PATH.name, DEMO_PATH.read_bytes())
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"Демонстрационный JSON недоступен или повреждён: {exc}") from exc
    bonds = loaded.bonds
    if (len(bonds) != 5 or {b.name.casefold() for b in bonds} != DEMO_NAMES
            or loaded.assessment_date != DEMO_DATE
            or loaded.weight_mode != "По рыночной стоимости"
            or any(b.quantity != 20000 or b.valuation_date != DEMO_DATE or not b.included for b in bonds)):
        raise ValueError("Демонстрационный JSON не соответствует составу, дате, количествам или весам задания.")
    errors = validate_portfolio(bonds).errors
    result = portfolio_analytics(bonds, loaded.weight_mode, loaded.calculation_mode)
    if errors or not result["complete"]:
        raise ValueError("Демонстрационный портфель не готов: " + "; ".join(errors + result["errors"]))
    return loaded


def reset_portfolio(state, loaded=None, *, demo=False):
    """Clear only this visitor's dependent UI, never other session objects."""
    ids = [b.record_id for b in state.get("bonds", [])]
    prefixes = ("manual_add_", "compact_", "individual_", "_yield_", "_pricing_",
                "last_price_", "price_", "yield_", "parameters_", "schedule_",
                "bond_calculation_mode_", "previous_calculation_mode_")
    keys = {"scenarios", "pending_restore", "import_notice", "selected_record_id", "analysis_record",
            "portfolio_record", "added_record_id", "overview_shock", "stress_bp", "risk_limit_pct",
            "individual_shifts", "position_risk_sort", "clear_confirm", "restore_confirm", "demo_error",
            "active_shock_bp", "active_individual_shocks"}
    for key in list(state):
        if key in keys or key.startswith(prefixes) or any(record_id in key for record_id in ids):
            state.pop(key, None)
    state["bonds"] = loaded.bonds if loaded else []
    state["scenarios"] = loaded.scenarios if loaded else []
    state["revision"] = state.get("revision", 0) + 1
    state["demo_initialized"] = True
    source = loaded if demo else None
    if loaded and source is None:
        try:
            source = load_demo()
        except ValueError:
            pass
    source_ids = {b.record_id for b in source.bonds} if source else set()
    state["demo_record_ids"] = [b.record_id for b in loaded.bonds if b.record_id in source_ids] if loaded else []
    state["demo_fingerprint"] = demo_fingerprint(source.bonds) if source else None
    if loaded:
        for key in ("weight_mode", "assessment_date", "calculation_mode"):
            state[key] = getattr(loaded, key)


def initialize_demo(state):
    if state.get("demo_initialized"):
        return
    # Existing sessions/imports must not be replaced during a hot reload.
    already_has_portfolio = "bonds" in state
    state["demo_initialized"] = True
    if already_has_portfolio:
        return
    try:
        reset_portfolio(state, load_demo(), demo=True)
    except ValueError as exc:
        state["demo_error"] = str(exc)


def preserve_record_widgets(state):
    """Detach explicit widget keys from Streamlit's page-cleanup lifecycle.

    Values remain UI drafts; Bond remains the single canonical saved record.
    Canonical-value synchronization still takes priority after table/import edits.
    """
    ids = [b.record_id for b in state.get("bonds", [])]
    # Button/upload widget state is read-only; preserve only value widgets.
    prefixes = ("yield_value_", "price_value_", "price_type_", "price_unit_",
                "pricing_method_", "parameters_", "schedule_mode_", "bond_calculation_mode_",
                "remove_confirm_")
    for key in list(state):
        manual = key.startswith("manual_add_") and key not in {"manual_add_confirm", "manual_add_edit"}
        if manual or (key.startswith(prefixes) and any(record_id in key for record_id in ids)):
            state[key] = state[key]

