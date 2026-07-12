from __future__ import annotations

from functools import lru_cache
from typing import FrozenSet, Set

from agriconnect.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG

_BUYER_BLOCKED_PREFIXES = (
    "STOCK_",
    "SALES_",
    "AGRO_",
    "FINANCE_",
    "FARM_",
)

_PRODUCER_BLOCKED_PREFIXES = (
    "BUYER_",
    "PROCUREMENT_",
)

_COMMON_TOOL_EXTRAS: Set[str] = {
    "create_agent_action",
    "get_user_by_phone",
    # Moderation / anti-abuse — cross-role (gate d'entrée + captation demande)
    "get_account_status",
    "get_prohibited_terms",
    "record_moderation_strike",
    "record_demand_signal",
}

_BUYER_TOOL_EXTRAS: Set[str] = {
    "search_products",
    "validate_stock_availability_atomic",
    "create_preorder_draft",
    "confirm_preorder_draft",
    "update_negotiation_offer",
    "select_winning_bid",
    "get_auction_bids",
    "close_negotiation_session",
    "initiate_negotiation_session",
    "get_buyer_orders_dashboard",
    "cancel_pending_order",
    "get_transaction_summary",
}

_PRODUCER_TOOL_EXTRAS: Set[str] = {
    "get_producer_farm",
    "get_producer_orders",
    "get_auctions",
    "get_auctions_bids",
    # Cycle enchères producteur : découverte par catégorie + suivi des offres.
    "get_producer_auctions",
    "get_my_active_bids",
    "place_bid",
}

_EXTRA_ROLE_TOOLS = {
    "BUYER": _COMMON_TOOL_EXTRAS | _BUYER_TOOL_EXTRAS,
    "PRODUCER": _COMMON_TOOL_EXTRAS | _PRODUCER_TOOL_EXTRAS,
}


def normalize_role(role: str | None) -> str:
    role_up = str(role or "").upper().strip()
    if role_up in {"BUYER", "ACHETEUR", "ACHETEUSE"}:
        return "BUYER"
    return "PRODUCER"


def _is_goal_blocked_for_role(role: str, goal: str) -> bool:
    goal_up = str(goal or "").upper().strip()
    if not goal_up:
        return False
    if role == "BUYER":
        return goal_up.startswith(_BUYER_BLOCKED_PREFIXES)
    return goal_up.startswith(_PRODUCER_BLOCKED_PREFIXES)


def is_goal_allowed(role: str | None, goal: str | None) -> bool:
    role_norm = normalize_role(role)
    goal_up = str(goal or "").upper().strip()
    if not goal_up:
        return True
    return not _is_goal_blocked_for_role(role_norm, goal_up)


@lru_cache(maxsize=None)
def get_allowed_goals(role: str | None) -> FrozenSet[str]:
    role_norm = normalize_role(role)
    allowed = {
        intent_key.upper()
        for intent_key in INTENT_CONFIG.keys()
        if is_goal_allowed(role_norm, intent_key)
    }
    return frozenset(allowed)


def _intent_tools_for_role(role: str) -> Set[str]:
    tools: Set[str] = set()
    for intent_key, cfg in INTENT_CONFIG.items():
        if not is_goal_allowed(role, intent_key):
            continue
        tool_name = cfg.get("tool_name")
        if isinstance(tool_name, str) and tool_name:
            tools.add(tool_name)
    return tools


@lru_cache(maxsize=None)
def get_allowed_tools(role: str | None) -> FrozenSet[str]:
    role_norm = normalize_role(role)
    base_tools = _intent_tools_for_role(role_norm)
    extras = _EXTRA_ROLE_TOOLS.get(role_norm, set())
    return frozenset(tool for tool in base_tools.union(extras) if tool)


def is_tool_allowed(role: str | None, tool_name: str | None) -> bool:
    if not tool_name:
        return True
    role_norm = normalize_role(role)
    tool = str(tool_name or "").strip()
    if not tool:
        return True
    return tool in get_allowed_tools(role_norm)


__all__ = [
    "get_allowed_goals",
    "get_allowed_tools",
    "is_goal_allowed",
    "is_tool_allowed",
    "normalize_role",
]
