"""Formation — DRY Form Node.

Single LangGraph node that handles conversational form collection for
the FormationCoach agent (e.g. crop cycle creation).

When the form completes, it maps collected data into the Formation state
and triggers the appropriate downstream action.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from agriconnect.agents.forms import (
    CROP_CYCLE_FORM,
    FORM_REGISTRY,
    run_form_step,
)

logger = logging.getLogger("AgriConnect.Formation.FormNode")


async def formation_form_node(state: Dict[str, Any], agent_config: Any = None) -> Dict[str, Any]:
    """LangGraph node: drives the DRY form engine for FormationCoach."""
    form_id = state.get("active_form")
    if not form_id:
        # Auto-activate crop cycle form (we only reach here if routing decided it)
        form_id = CROP_CYCLE_FORM.form_id
        logger.info("[FormNode] Auto-activating form=%s", form_id)

    spec = FORM_REGISTRY.get(form_id)
    if spec is None:
        logger.warning("[FormNode] Unknown form_id=%s — clearing.", form_id)
        return {"active_form": None, "form_step": None}

    # Merge extracted info from the current user query
    extracted: Dict[str, Any] = {}
    # Pull from learner_profile, crop_name, zone, etc.
    for key in ("identified_crop", "crop_name", "zone_category", "area_ha", "sowing_date"):
        val = state.get(key)
        if val not in (None, "", [], {}):
            extracted[key] = val

    norm = (state.get("user_query") or "").strip()
    current_step = state.get("form_step")

    # Handle confirmation flow
    user_query_lower = norm.lower()
    if current_step == "CONFIRMING":
        if user_query_lower in ("oui", "yes", "ok", "confirmer", "c'est bon", "correct"):
            form_data = dict(state.get("form_data") or {})
            logger.info("[FormNode] Crop cycle form confirmed.")
            return {
                "active_form": None,
                "form_step": "COMPLETE",
                "form_data": form_data,
                # Map form data back to Formation state fields
                "identified_crop": form_data.get("identified_crop", ""),
                "zone_category": form_data.get("zone_category", ""),
                "area_ha": float(form_data.get("area_ha", 0)),
                "status": "VALIDATED",
                "response_strategy": "PROVIDE_ANSWER",
            }
        elif user_query_lower in ("non", "no", "annuler", "cancel"):
            logger.info("[FormNode] Crop cycle form cancelled.")
            return {
                "active_form": None,
                "form_step": None,
                "form_data": {"__reset__": True},
                "status": "COMPOSED",
                "response_strategy": "PROVIDE_ANSWER",
                "final_response": "Création du cycle annulée. Que souhaitez-vous faire ?",
            }

    # If we have a specific step and raw text, inject as that slot value
    if current_step and current_step not in ("CONFIRMING", "COMPLETE") and norm:
        if current_step not in extracted:
            extracted[current_step] = norm

    result = run_form_step(spec, state, extracted)
    return result.patch
