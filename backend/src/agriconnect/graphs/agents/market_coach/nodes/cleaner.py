from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from agriconnect.graphs.agents.market_coach.core.state import MarketAgentState
from agriconnect.graphs.agents.market_coach.utils import CANONICAL_TRANSACTION_FIELDS

_MAX_CHAT_HISTORY = 4
_ACTIVE_GOAL_STATES = frozenset({"ACTIVE", "WAITING_INPUT", "WAITING_CONFIRMATION", "EXECUTING"})
_EPHEMERAL_WORKING_KEYS = ("payload_richness", "last_confidence", "step_index")
_ONBOARDING_TRANSACTION_FIELDS = frozenset({"name", "zone_name", "zone_id", "phone"})


def _trim_history(items: Any, limit: int) -> Optional[List[Any]]:
    if isinstance(items, list) and len(items) > limit:
        return items[-limit:]
    return None


def _sanitize_transaction_payload(payload: Any, state: MarketAgentState) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    allowed: Set[str] = set(CANONICAL_TRANSACTION_FIELDS)
    if state.get("is_onboarding") or str(state.get("response_strategy") or "").upper() == "ONBOARDING":
        allowed.update(_ONBOARDING_TRANSACTION_FIELDS)
    sanitized = {
        key: value
        for key, value in payload.items()
        if key in allowed and value not in (None, "", [], {})
    }
    return sanitized if sanitized != payload else None


async def state_cleaner_node(
    state: MarketAgentState,
    *_: Any,
    **__: Any,
) -> Dict[str, Any]:
    """Garbage collector executed at the very end of each turn."""

    patch: Dict[str, Any] = {}

    working = dict(state.get("working_memory") or {})
    working_patch = False

    if working.get("recent_corrections") not in (None, {}):
        working["recent_corrections"] = {}
        working_patch = True

    if working.get("fetched_data_cache") is not None:
        working["fetched_data_cache"] = None
        working_patch = True
    elif "fetched_data_cache" not in working:
        working["fetched_data_cache"] = None
        working_patch = True

    for key in _EPHEMERAL_WORKING_KEYS:
        if working.pop(key, None) is not None:
            working_patch = True

    if working_patch:
        patch["working_memory"] = working

    trimmed_history = _trim_history(state.get("chat_history"), _MAX_CHAT_HISTORY)
    if trimmed_history is not None:
        patch["chat_history"] = trimmed_history

    payload_patch = _sanitize_transaction_payload(state.get("transaction_payload"), state)
    if payload_patch is not None:
        patch["transaction_payload"] = payload_patch

    draft_payload = state.get("draft_payload")
    if isinstance(draft_payload, dict) and draft_payload and not draft_payload.get("__reset__"):
        goal_status = str(state.get("goal_status") or "").upper()
        current_goal = str(state.get("current_goal") or "").upper()
        transaction_active = goal_status in _ACTIVE_GOAL_STATES and current_goal == "BUYER_ADD_TO_CART"
        status_flag = str(state.get("status") or "").upper()
        if not transaction_active or status_flag in {"FAILED", "COMPLETED"}:
            patch["draft_payload"] = {"__reset__": True}

    return patch


__all__ = ["state_cleaner_node"]