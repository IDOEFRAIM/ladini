"""Market — Intent router (response_strategy).

Ce module isole la logique d'aiguillage AG-UI (routing) afin d'alléger
`shared_core.py`.

Contrat:
- `response_strategy(state, mc_runtime) -> updates`
- N'émet pas d'appels MCP (pur routing déterministe).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from agriconnect.graphs.agents.market_coach.core.slots import SLOT_FILLING_INPUTS

logger = logging.getLogger("AgriConnect.Market.IntentRouter")


async def response_strategy(state: Dict[str, Any], mc_runtime: Any) -> Dict[str, Any]:
    """Routeur AG-UI agentic — détermine la stratégie de réponse en tenant
    compte de la décision cognitive, de la progression, et du contexte."""
    status = str(state.get("status") or "").upper().strip()
    working = state.get("working_memory") or {}
    current_goal = state.get("current_goal") or working.get("active_goal") or working.get("locked_intent")
    expected_input = state.get("expected_input")
    interpreted_event = state.get("interpreted_event")
    missing_fields = state.get("missing_fields") or []
    last_missing_field = state.get("last_missing_field")
    existing_strategy = str(state.get("response_strategy") or "").upper().strip()
    if state.get("slot_enrichment_force_clarification"):
        reasons = state.get("clarification_reasons") or []
        logger.warning(
            "[ResponseStrategy] slot enrichment forced clarification | reasons=%s",
            reasons,
        )
        updates: Dict[str, Any] = {
            "response_strategy": "CLARIFICATION",
            "status": "WAITING_INPUT",
            "ag_ui_component": None,
            "clarification_reasons": reasons,
        }
        return updates
    if existing_strategy == "ONBOARDING" or state.get("is_onboarding"):
        return {
            "response_strategy": "ONBOARDING",
            "status": "WAITING_INPUT" if state.get("is_onboarding") else status or "WAITING_INPUT",
            "ag_ui_component": state.get("ag_ui_component"),
        }
    cognitive = state.get("cognitive_decision") or {}
    cognitive_action = cognitive.get("action", "")

    # Priority 1: security + global commands (interrupt/cancel) must beat missing_fields.
    if state.get("security_status") == "SCAM_DETECTED":
        return {"response_strategy": "ERROR", "status": "BLOCKED", "ag_ui_component": None}

    if state.get("interruption_detected") or interpreted_event == "INTERRUPTION" or existing_strategy == "INTERRUPTION_HANDLER":
        # If upstream nodes already produced a concrete UI (menu/form), keep it.
        if existing_strategy in {"SELECTION_MENU", "ASK_MISSING_FIELD", "ERROR", "CONFIRMATION", "SUCCESS"}:
            updates: Dict[str, Any] = {"response_strategy": existing_strategy}
            if existing_strategy == "CONFIRMATION":
                updates["status"] = "WAITING_CONFIRMATION"
            if existing_strategy == "ASK_MISSING_FIELD":
                updates["status"] = "WAITING_INPUT"
            return updates
        if expected_input == "SELECTION" and (state.get("pending_menu") or state.get("expected_candidates")):
            return {"response_strategy": "SELECTION_MENU", "status": "WAITING_INPUT", "ag_ui_component": None}
        if expected_input == "CONFIRMATION":
            return {"response_strategy": "CONFIRMATION", "status": "WAITING_CONFIRMATION", "ag_ui_component": None}
        return {"response_strategy": "INTERRUPTION_HANDLER", "ag_ui_component": None}

    # Global explicit cancel/refusal should never be blocked by missing_fields.
    if interpreted_event == "REJECT":
        return {"response_strategy": "CLARIFICATION", "status": "WAITING_INPUT", "ag_ui_component": None}

    # --- COGNITIVE GUARD DECISIONS take priority (after global commands) ---
    if cognitive_action == "recover_active_tunnel":
        # Prefer re-showing menus/confirmations instead of verbose coaching.
        if expected_input == "SELECTION" and (state.get("pending_menu") or state.get("expected_candidates")):
            return {"response_strategy": "SELECTION_MENU", "status": "WAITING_INPUT", "ag_ui_component": None}
        if expected_input == "CONFIRMATION":
            return {"response_strategy": "CONFIRMATION", "status": "WAITING_CONFIRMATION", "ag_ui_component": None}
        return {"response_strategy": "RECOVERY", "status": "WAITING_INPUT", "ag_ui_component": None}
    if cognitive_action == "abandon_tunnel_max_retries":
        return {"response_strategy": "CLARIFICATION", "ag_ui_component": None}

    if interpreted_event in {"UNKNOWN", "OUT_OF_SCOPE"}:
        if expected_input == "SELECTION" and state.get("expected_candidates"):
            return {"response_strategy": "SELECTION_MENU", "status": "WAITING_INPUT", "ag_ui_component": None}
        if expected_input in SLOT_FILLING_INPUTS:
            return {"response_strategy": "ASK_MISSING_FIELD", "status": "WAITING_INPUT", "ag_ui_component": None}
        if expected_input == "CONFIRMATION":
            return {"response_strategy": "CONFIRMATION", "status": "WAITING_CONFIRMATION", "ag_ui_component": None}
        # Outside any slot-filling context, never fall through to SUCCESS based on a stale
        # previous status/execution_result. UNKNOWN should always clarify.
        return {"response_strategy": "CLARIFICATION", "status": "WAITING_INPUT", "ag_ui_component": None}

    # Now it's safe to enforce slot-filling.
    if missing_fields or last_missing_field:
        return {"response_strategy": "ASK_MISSING_FIELD", "status": "WAITING_INPUT", "ag_ui_component": None}

    # Respect upstream-set strategy (ex: buyer_flow nodes returning menus).
    if existing_strategy in {"SELECTION_MENU", "ASK_MISSING_FIELD", "ERROR", "CONFIRMATION", "SUCCESS"}:
        updates: Dict[str, Any] = {"response_strategy": existing_strategy}
        if state.get("ag_ui_component") is not None:
            updates["ag_ui_component"] = state.get("ag_ui_component")
        if existing_strategy == "CONFIRMATION":
            updates["status"] = "WAITING_CONFIRMATION"
        if existing_strategy == "ASK_MISSING_FIELD":
            updates["status"] = "WAITING_INPUT"
        return updates

    # Respect upstream-set RECOVERY (e.g. from cognitive_guard)
    if existing_strategy == "RECOVERY":
        return {"response_strategy": "RECOVERY", "status": "WAITING_INPUT", "ag_ui_component": None}

    if status in {"BLOCKED", "ERROR"}:
        return {"response_strategy": "ERROR", "ag_ui_component": None}

    if status == "COMPLETED" or (not current_goal and state.get("execution_result")):
        return {"response_strategy": "SUCCESS", "ag_ui_component": None}

    if status == "WAITING_CONFIRMATION" or expected_input == "CONFIRMATION":
        return {"response_strategy": "CONFIRMATION", "status": "WAITING_CONFIRMATION", "ag_ui_component": None}

    if status == "WAITING_INPUT" or expected_input not in {None, "NONE"}:
        if expected_input == "SELECTION" and state.get("expected_candidates"):
            return {"response_strategy": "SELECTION_MENU", "status": "WAITING_INPUT", "ag_ui_component": None}
        if state.get("missing_fields") or state.get("last_missing_field") or expected_input:
            return {"response_strategy": "ASK_MISSING_FIELD", "status": "WAITING_INPUT", "ag_ui_component": None}

    return {"response_strategy": "CLARIFICATION", "ag_ui_component": None}
