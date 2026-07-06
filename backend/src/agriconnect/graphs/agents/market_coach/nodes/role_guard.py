from __future__ import annotations

from typing import Any, Dict, Optional

from agriconnect.graphs.roles import (
    get_allowed_goals,
    get_allowed_tools,
    is_goal_allowed,
    is_tool_allowed,
    normalize_role,
)


def _build_error(final_response: str) -> Dict[str, Any]:
    return {
        "status": "ERROR",
        "response_strategy": "ERROR",
        "final_response": final_response,
        "ag_ui_component": None,
        "validation_errors": ["role_violation"],
    }


def _extract_goal_candidates(state: Dict[str, Any]) -> Dict[str, Optional[str]]:
    working_memory = state.get("working_memory") or {}
    return {
        "current_goal": state.get("current_goal"),
        "active_goal": working_memory.get("active_goal"),
        "locked_intent": working_memory.get("locked_intent"),
        "detected_intent": state.get("detected_intent"),
    }


def _extract_tool_candidates(state: Dict[str, Any]) -> Dict[str, Optional[str]]:
    payload = state.get("transaction_payload") or {}
    return {
        "selected_tool": state.get("selected_tool"),
        "requested_tool": payload.get("tool_name") or payload.get("requested_tool"),
        "pending_tool": (state.get("executor_result") or {}).get("tool_name"),
    }


def make_role_guard(role: str):
    role_norm = normalize_role(role)
    allowed_goals = get_allowed_goals(role_norm)
    allowed_tools = get_allowed_tools(role_norm)

    async def _role_guard(state: Dict[str, Any], _: Any) -> Dict[str, Any]:
        patch: Dict[str, Any] = {}

        state_role = state.get("role") or state.get("user_role")
        if state_role:
            state_role_norm = normalize_role(state_role)
            if state_role_norm != role_norm:
                return _build_error(
                    "Votre session est verrouillée sur un autre rôle utilisateur."
                )
        else:
            patch["role"] = role_norm
            patch.setdefault("user_role", role_norm)

        # Ensure user_role is aligned even if provided
        if (state.get("user_role") or "").strip() == "":
            patch["user_role"] = role_norm

        goal_candidates = _extract_goal_candidates(state)
        for source, goal in goal_candidates.items():
            if not goal:
                continue
            goal_up = str(goal).upper()
            if goal_up not in allowed_goals and not is_goal_allowed(role_norm, goal_up):
                return _build_error(
                    "Cette action est réservée à un autre rôle utilisateur."
                )

        tool_candidates = _extract_tool_candidates(state)
        for _, tool in tool_candidates.items():
            if not tool:
                continue
            if tool not in allowed_tools and not is_tool_allowed(role_norm, tool):
                return _build_error(
                    "Cet outil n'est pas autorisé pour votre profil."
                )

        return patch

    return _role_guard


__all__ = ["make_role_guard"]
