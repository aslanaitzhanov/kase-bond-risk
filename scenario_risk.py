"""Interpret existing full-revaluation results without changing pricing."""
from __future__ import annotations

import math


def assess_scenario(result, base_value, limit_pct, *, complete):
    values = (base_value, limit_pct, result.get("exact_change_kzt"))
    if (not complete or not result.get("valid") or
            any(v is None or not math.isfinite(float(v)) for v in values) or
            base_value <= 0 or limit_pct < 0):
        return None
    change_pct = result["exact_change_kzt"] / base_value * 100
    decline = max(0.0, -change_pct)
    # Compare unrounded values. Equality is within the selected limit.
    return {"change_pct": change_pct, "change_kzt": result["exact_change_kzt"],
            "decline_pct": decline, "limit_pct": limit_pct,
            "exceeded": decline > limit_pct, "margin_pp": limit_pct - decline}


def position_risk_rows(result, analytics):
    if not result.get("valid"):
        return []
    return [{"Инструмент": row["name"],
             "Изменение цены, %": (row["new_dirty_price"] / row["old_dirty_price"] - 1) * 100,
             "Изменение стоимости, ₸": row["exact_pnl_kzt"],
             "Модиф. дюрация, лет": analytics[row["position_key"]]["modified_duration_years"]}
            for row in result["positions"]]
