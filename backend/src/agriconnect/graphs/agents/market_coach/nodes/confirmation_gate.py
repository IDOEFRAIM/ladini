from typing import Any, Dict

from agriconnect.graphs.agents.market_coach.core.base import get_node_logger, _READ_GOALS
from agriconnect.graphs.agents.market_coach.services.ui.confirmation_summary import (
    build_confirmation_summary as _build_confirmation_summary,
)

logger = get_node_logger("ConfirmationGateNode")


async def confirmation_gate(state: Dict[str, Any], mc_runtime: Any) -> Dict[str, Any]:
    """Gère l'état d'approbation explicite avant l'écriture en base de données."""
    goal = (state.get("current_goal") or "").upper()
    payload: Dict[str, Any] = state.get("transaction_payload") or {}
    event = str(state.get("interpreted_event") or "").upper()

    if goal in _READ_GOALS:
        return {
            "is_certified": True,
            "execution_authorized": True,
            "waiting_for_confirmation": False,
            "status": "EXECUTING",
            "ag_ui_component": None,
        }

    if state.get("waiting_for_confirmation"):
        if event == "CONFIRM":
            return {
                "is_certified": True,
                "execution_authorized": True,
                "waiting_for_confirmation": False,
                "status": "EXECUTING",
                "ag_ui_component": None,
            }
        if event == "REJECT":
            return {
                "is_certified": False,
                "execution_authorized": False,
                "waiting_for_confirmation": False,
                "current_goal": None,
                "transaction_payload": {"__reset__": True},
                "missing_fields": [],
                "completed_fields": [],
                "goal_status": "COMPLETED",
                "status": "COMPLETED",
                "response_strategy": "CLARIFICATION",
                "final_response": "Opération annulée. Que souhaitez-vous faire ?",
                "ag_ui_component": None,
            }

    summary = _build_confirmation_summary(goal, payload)
    return {
        "waiting_for_confirmation": True,
        "is_certified": False,
        "execution_authorized": False,
        "confirmation_summary": summary,
        "expected_input": "CONFIRMATION",
        "last_agent_question": summary,
        "status": "WAITING_CONFIRMATION",
        "goal_status": "WAITING_CONFIRMATION",
        "response_strategy": "CONFIRMATION",
        "ag_ui_component": {
            "lc_type": "constructor",
            "id": ["ag_ui", "FormConfirmation"],
            "kwargs": {
                "title": "Confirmation requise",
                "summary": summary,
                "submit_label": "Confirmer",
                "cancel_label": "Annuler",
                "metadata": {"goal": goal},
            },
        },
    }
