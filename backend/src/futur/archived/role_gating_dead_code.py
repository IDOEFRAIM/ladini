"""Ancien mécanisme de blocage L2/L3 par préfixe de rôle (`graphs/roles.py`).

Retiré de `src/agriconnect/graphs/roles.py` le 2026-08 : confirmé CODE MORT
par grep exhaustif sur tout `src/agriconnect` — aucun appelant vivant pour
`is_goal_allowed`/`is_tool_allowed`/`get_allowed_goals`/`get_allowed_tools`
(seul `normalize_role`, resté dans `graphs/roles.py`, est encore utilisé —
pour le routage UI/workspace par défaut, pas pour la sécurité).

La refonte double-rôle (un même utilisateur vend ET achète, graphe MCP
unifié) a rendu ce blocage par préfixe de goal/tool obsolète. La frontière
de sécurité RÉELLE aujourd'hui est l'épinglage d'IDENTITÉ à la session dans
`services/mcp/schema_resolver.py::lookup_arg_value` (jamais au payload/texte
libre) — voir `tests/chaos/test_role_isolation.py` pour la couverture actuelle
et son test sentinelle `test_prefix_based_role_gate_is_confirmed_dead_code`,
qui échoue si ce mécanisme redevient vivant quelque part dans `src/agriconnect`.

Conservé ici (pas supprimé) au cas où un blocage par rôle plus grossier
redeviendrait pertinent un jour (ex: un rôle "invité" restreint). Réintégrer
uniquement après confirmation d'un besoin production réel — voir
`src/futur/archived/README.md`.
"""
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
    # Réservation de production future (précommande liée à MarketOffer).
    "reserve_future_offer",
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
    "update_bid_price",
    # Vue des réservations reçues sur les productions futures.
    "get_offer_reservations",
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
