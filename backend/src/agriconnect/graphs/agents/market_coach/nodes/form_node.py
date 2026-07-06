"""Market — DRY Form Node.

Single LangGraph node that handles ALL conversational form collection
for the MarketCoach agent (product creation, auction creation, etc.).

It detects `active_form` in state, delegates to the shared form engine,
and returns a state patch. When the form completes, it maps the collected
data back into `transaction_payload` and sets `current_goal` so the
existing confirmation_gate → mcp_tool_executor pipeline can proceed.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from agriconnect.agents.forms import (
    FormSpec,
    SlotSpec,
    FORM_REGISTRY,
    run_form_step,
)

logger = logging.getLogger("AgriConnect.Market.FormNode")

# Maps form_id → current_goal to set on completion
_FORM_TO_GOAL: Dict[str, str] = {
    "PRODUCT_CREATE": "SALES_PUBLISH_PRODUCT",
    "AUCTION_CREATE": "PROCUREMENT_CREATE_REQUEST",
}

# Reverse: goal → form_id (for auto-activation from goal_planner)
_GOAL_TO_FORM: Dict[str, str] = {v: k for k, v in _FORM_TO_GOAL.items()}


def _infer_slot_from_text(user_text: str, spec: FormSpec) -> Optional[SlotSpec]:
    if not user_text:
        return None
    lowered = user_text.lower()
    for slot in spec.slots:
        candidates = [slot.name.replace("_", " "), slot.label or ""]
        candidates.extend(slot.aliases or [])
        for candidate in candidates:
            if not candidate:
                continue
            candidate_norm = str(candidate).lower().strip()
            if candidate_norm and candidate_norm in lowered:
                return slot
    return None


async def form_node(state: Dict[str, Any], mc_runtime: Any) -> Dict[str, Any]:
    """LangGraph node: drives the DRY form engine for MarketCoach."""
    form_id = state.get("active_form")

    # Guard: if the form already reached COMPLETE we must not re-run it.
    if state.get("form_step") == "COMPLETE" and form_id is None:
        logger.debug("[FormNode] Form already complete, skipping re-entry.")
        return {}

    # Auto-activate form from current_goal if not yet set
    if not form_id:
        goal = state.get("current_goal")
        form_id = _GOAL_TO_FORM.get(goal) if goal else None
        if form_id:
            logger.info("[FormNode] Auto-activating form=%s from goal=%s", form_id, goal)
        else:
            logger.debug("[FormNode] No active form — pass-through.")
            return {}

    spec = FORM_REGISTRY.get(form_id)
    if spec is None:
        logger.warning("[FormNode] Unknown form_id=%s — clearing.", form_id)
        return {"active_form": None, "form_step": None}

    # Merge extracted_entities as the new user input for this turn
    extracted = dict(state.get("extracted_entities") or {})
    # Also check normalized_text for direct value injection
    norm = (state.get("normalized_text") or state.get("user_query") or "").strip()
    current_step = state.get("form_step")

    # If user is confirming (the form asked for confirmation last turn)
    event = str(state.get("interpreted_event") or "").upper()
    if current_step == "CONFIRMING":
        if event == "CONFIRM":
            # User confirmed → complete the form
            form_data = dict(state.get("form_data") or {})
            goal = _FORM_TO_GOAL.get(form_id)
            logger.info("[FormNode] Form %s confirmed → goal=%s", form_id, goal)
            return {
                "active_form": None,
                "form_step": "COMPLETE",
                "form_data": form_data,
                "transaction_payload": form_data,
                "current_goal": goal,
                "goal_status": "ACTIVE",
                "status": "WAITING_CONFIRMATION",
                "waiting_for_confirmation": True,
                "execution_authorized": False,
                "missing_fields": [],
            }
        elif event == "REJECT":
            logger.info("[FormNode] Form %s cancelled by user.", form_id)
            return {
                "active_form": None,
                "form_step": None,
                "form_data": {"__reset__": True},
                "current_goal": None,
                "goal_status": "COMPLETED",
                "status": "COMPLETED",
                "response_strategy": "CLARIFICATION",
                "final_response": "Formulaire annulé. Que souhaitez-vous faire ?",
                "ag_ui_component": None,
            }
        else:
            # User wants to correct a field instead of confirming
            correction_payload = dict(extracted)
            has_explicit_slot_value = any(
                correction_payload.get(slot.name) not in (None, "", [], {}) for slot in spec.slots
            )
            inferred_slot = _infer_slot_from_text(norm, spec) if spec else None

            if not has_explicit_slot_value and not inferred_slot:
                return {
                    "status": "WAITING_INPUT",
                    "response_strategy": "ASK_MISSING_FIELD",
                    "final_response": (
                        "Pour corriger un champ, tapez par exemple « prix 480000 » ou "
                        "« date 30 juillet ». Sinon répondez « oui » pour confirmer ou « annuler » pour quitter."
                    ),
                    "ag_ui_component": None,
                }

            correction_state = dict(state)
            correction_state["form_step"] = None
            correction_state["status"] = "WAITING_INPUT"
            correction_state["waiting_for_confirmation"] = False
            correction_state["ag_ui_component"] = None
            correction_state.pop("confirmation_summary", None)
            correction_state.pop("last_agent_question", None)
            correction_state.pop("final_response", None)

            correction_data = dict(correction_state.get("form_data") or {})
            correction_state["form_data"] = correction_data
            if inferred_slot:
                correction_data.pop(inferred_slot.name, None)
                if inferred_slot.name not in correction_payload and norm:
                    correction_payload[inferred_slot.name] = norm

            result = run_form_step(spec, correction_state, correction_payload)
            patch = dict(result.patch or {})
            if patch:
                patch.setdefault("ag_ui_component", None)
                return patch

            return {
                "status": "WAITING_INPUT",
                "response_strategy": "ASK_MISSING_FIELD",
                "final_response": (
                    "Indiquez la valeur à corriger (ex: « quantité 20 tonnes ») ou tapez « annuler »."
                ),
                "ag_ui_component": None,
            }

    # If we have a specific form_step and normalized text,
    # inject it as the expected slot value
    if current_step and current_step not in ("CONFIRMING", "COMPLETE") and norm:
        if current_step not in extracted:
            extracted[current_step] = norm

    result = run_form_step(spec, state, extracted)

    patch = dict(result.patch or {})
    if result.is_complete or patch.get("form_step") == "COMPLETE":
        form_data = dict(patch.get("form_data") or state.get("form_data") or {})
        goal = _FORM_TO_GOAL.get(form_id)
        logger.info("[FormNode] Form %s complete → goal=%s", form_id, goal)
        patch.update({
            "active_form": None,
            "form_step": "COMPLETE",
            "form_data": form_data,
            "transaction_payload": form_data,
            "current_goal": goal,
            "goal_status": "ACTIVE",
            "status": "WAITING_CONFIRMATION",
            "waiting_for_confirmation": True,
            "execution_authorized": False,
            "missing_fields": [],
            "last_missing_field": None,
            "expected_input": "NONE",
            "response_strategy": "CONFIRMATION",
            "final_response": state.get("confirmation_summary") or patch.get("confirmation_summary"),
            "ag_ui_component": None,
        })

    return patch
