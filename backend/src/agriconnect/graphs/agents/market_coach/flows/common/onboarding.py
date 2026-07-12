"""Onboarding graph node — bridges the unified state machine to LangGraph.

Reconstructs ``OnboardingState`` from *all* available state sources with a
strict priority chain (``onboarding_profile`` > ``transaction_payload`` >
top-level state keys) so that data collected in a prior turn is never lost
even if individual state keys are cleared between turns.
"""
from typing import Any, Dict, Optional

from agriconnect.core.logger import get_logger
from agriconnect.agents import (
    OnboardingState,
    OnboardingStep,
    run_onboarding_step,
)
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    _llm_extract_onboarding_all,
)

logger = get_logger("AgriConnect.MarketCoach.Onboarding")


def _pick(key: str, *sources: Dict[str, Any]) -> Any:
    """Return the first non-empty value for *key* across multiple dicts."""
    for src in sources:
        val = src.get(key)
        if val not in (None, "", [], {}):
            return val
    return None


async def onboarding_node(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    in_onboarding = bool(state.get("is_onboarding"))
    existing_strategy = str(state.get("response_strategy") or "").upper()
    if not in_onboarding and existing_strategy != "ONBOARDING":
        return {}

    ob_profile = dict(state.get("onboarding_profile") or {})
    tx_payload = dict(state.get("transaction_payload") or {})

    phone = _pick("phone", ob_profile, tx_payload, state) or state.get("user_phone") or state.get("phone_number")

    step_name = state.get("onboarding_internal_step") or state.get("onboarding_step")
    if not step_name:
        has_role = _pick("role", ob_profile, tx_payload) or state.get("user_role")
        step_name = "COLLECT_NAME" if has_role else "COLLECT_ROLE"

    try:
        step = OnboardingStep(step_name)
    except ValueError:
        step = OnboardingStep.COLLECT_ROLE

    ob_state = OnboardingState(
        step=step,
        phone=str(phone).strip() if phone else None,
        name=_pick("name", ob_profile, tx_payload) or state.get("user_name"),
        role=_pick("role", ob_profile, tx_payload) or state.get("user_role"),
        zone_name=_pick("zone_name", ob_profile, tx_payload) or state.get("zone_name"),
        zone_id=_pick("zone_id", ob_profile, tx_payload) or state.get("zone_id"),
        prompt=str(state.get("onboarding_mode") or ""),
    )

    async def _llm_bulk(text: str) -> Dict[str, Optional[str]]:
        return await _llm_extract_onboarding_all(mc_runtime, text)

    user_text = (state.get("normalized_text") or state.get("user_query") or "").strip()
    extracted_entities = state.get("extracted_entities") or {}

    result = await run_onboarding_step(
        ob_state,
        user_text,
        mc_runtime,
        extracted_entities=extracted_entities,
        llm_extract_all=_llm_bulk,
    )

    updates = result.to_state_updates()
    completed = bool(result.state.completed)

    real_step_value = result.state.step.value
    updates["onboarding_internal_step"] = None if completed else real_step_value
    updates["onboarding_mode"] = None if completed else (result.state.prompt or None)

    display_complete = completed or result.state.step in {
        OnboardingStep.CONFIRM_DETAILS,
        OnboardingStep.CREATE_PROFILE,
        OnboardingStep.DONE,
    }
    updates["onboarding_step"] = "COMPLETED" if display_complete else real_step_value

    if completed:
        updates["response_strategy"] = "SUCCESS"
        updates["status"] = "COMPLETED"
        updates["is_onboarding"] = False
        updates["onboarding_mode"] = None
        if result.response_text:
            updates["final_response"] = result.response_text
        updates.setdefault("final_response", "Inscription terminee.")
    else:
        updates["response_strategy"] = "ONBOARDING"
        updates["is_onboarding"] = True

    if result.ag_ui_component:
        updates["ag_ui_component"] = result.ag_ui_component

    if result.response_text:
        updates["onboarding_prompt"] = result.response_text

    resolved = result.state
    updates["onboarding_profile"] = {
        "phone": resolved.phone or ob_state.phone,
        "name": resolved.name or ob_state.name,
        "role": resolved.role or ob_state.role,
        "zone_name": resolved.zone_name or ob_state.zone_name,
        "zone_id": resolved.zone_id or ob_state.zone_id,
    }
    updates["transaction_payload"] = dict(updates["onboarding_profile"])

    logger.info("[Onboarding] Step=%s completed=%s", resolved.step.value, resolved.completed)
    return updates


__all__ = ["onboarding_node"]
