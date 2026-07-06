"""Action handlers for the Finance domain."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from agriconnect.graphs.agents.market_coach.registry import register_action
from agriconnect.graphs.agents.market_coach.actions.common import require, require_phone, normalize_quantity_to_kg

@register_action("FINANCE_GET_SUMMARY", mode="READ")
def prep_finance_get_summary(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare le bilan comptable synthétique."""
    require_phone(state)
    farm_id = str(require(payload, "farm_id"))
    args: Dict[str, Any] = {"farm_id": farm_id}
    if payload.get("days"): args["days"] = int(payload["days"])
    return "get_expense_summary", args


@register_action("FINANCE_LOG_EXPENSE", mode="WRITE")
def prep_finance_log_expense(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    require_phone(state)
    farm_id = str(require(payload, "farm_id"))
    amount = float(require(payload, "price_mentioned"))
    return "add_expense", {
        "farm_id": farm_id,
        "label": str(payload.get("product") or "Dépense diverse"),
        "amount": amount,
        "category": str(payload.get("category") or "OTHER"),
    }
