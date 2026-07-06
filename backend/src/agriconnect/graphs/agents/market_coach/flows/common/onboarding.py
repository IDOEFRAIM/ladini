from typing import Any, Dict, Optional

from agriconnect.core.logging import get_logger
from agriconnect.agents import (
    OnboardingState,
    OnboardingStep,
    run_onboarding_step,
)
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    _llm_extract_onboarding_field,
)

logger = get_logger("AgriConnect.MarketCoach.Onboarding")

async def _market_llm_extract(mc_runtime: MarketRuntime, field: str, text: str) -> Optional[str]:
    return await _llm_extract_onboarding_field(mc_runtime, field, text)

async def onboarding_node(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    in_onboarding = bool(state.get("is_onboarding"))
    existing_strategy = str(state.get("response_strategy") or "").upper()
    if not in_onboarding and existing_strategy != "ONBOARDING":
        return {}

    payload = dict(state.get("transaction_payload") or {})
    onboarding_profile = dict(state.get("onboarding_profile") or {})
    phone = (
        onboarding_profile.get("phone")
        or payload.get("phone")
        or state.get("user_phone")
        or state.get("phone")
        or state.get("phone_number")
    )
    
    step_name = state.get("onboarding_internal_step") or state.get("onboarding_step")
    if not step_name:
        step_name = "COLLECT_ROLE" if not (payload.get("role") or state.get("user_role")) else "COLLECT_NAME"
    
    try:
        step = OnboardingStep(step_name)
    except ValueError:
        step = OnboardingStep.COLLECT_ROLE

    ob_state = OnboardingState(
        step=step,
        phone=str(phone).strip() if phone else None,
        name=onboarding_profile.get("name") or payload.get("name") or state.get("user_name"),
        role=onboarding_profile.get("role") or payload.get("role"),
        zone_name=onboarding_profile.get("zone_name") or payload.get("zone_name") or state.get("zone_name"),
        zone_id=onboarding_profile.get("zone_id") or payload.get("zone_id") or state.get("zone_id"),
        prompt=str(state.get("onboarding_mode") or ""),
    )

    async def _llm_extract(field: str, text: str) -> Optional[str]:
        return await _market_llm_extract(mc_runtime, field, text)

    user_text = (state.get("normalized_text") or state.get("user_query") or "").strip()
    extracted_entities = state.get("extracted_entities") or {}

    result = await run_onboarding_step(
        ob_state,
        user_text,
        mc_runtime,
        extracted_entities=extracted_entities,
        llm_extract=_llm_extract,
    )

    # Mise à jour des données de sortie
    updates = result.to_state_updates()
    completed = bool(result.state.completed)

    # Conserver l'étape réelle pour la prochaine itération
    real_step_value = result.state.step.value
    updates["onboarding_internal_step"] = None if completed else real_step_value
    updates["onboarding_mode"] = None if completed else (result.state.prompt or None)

    # Étape exposée publiquement (utilisée par les scripts de test / UI)
    display_complete = completed or result.state.step in {
        OnboardingStep.CONFIRM_DETAILS,
        OnboardingStep.CREATE_PROFILE,
        OnboardingStep.DONE,
    }
    public_step = "COMPLETED" if display_complete else real_step_value
    updates["onboarding_step"] = public_step

    if completed:
        # On force la stratégie SUCCESS afin que final_response rende le message d'onboarding.
        updates["response_strategy"] = "SUCCESS"
        updates["status"] = "COMPLETED"
        updates["is_onboarding"] = False
        updates["onboarding_mode"] = None
        if result.response_text:
            updates["final_response"] = result.response_text
        updates.setdefault("final_response", "✅ Inscription terminée.")
    else:
        updates["response_strategy"] = "ONBOARDING"
        updates["is_onboarding"] = True

    if result.ag_ui_component:
        updates["ag_ui_component"] = result.ag_ui_component

    if result.response_text:
        updates["onboarding_prompt"] = result.response_text

    updates["onboarding_profile"] = {
        "phone": result.state.phone or ob_state.phone,
        "name": result.state.name or ob_state.name,
        "role": result.state.role or ob_state.role,
        "zone_name": result.state.zone_name or ob_state.zone_name,
        "zone_id": result.state.zone_id or ob_state.zone_id,
    }

    # Mise à jour du payload avec les valeurs les plus récentes issues de l'état après traitement
    updates["transaction_payload"] = {
        "phone": result.state.phone or ob_state.phone,
        "name": result.state.name or ob_state.name,
        "role": result.state.role or ob_state.role,
        "zone_name": result.state.zone_name or ob_state.zone_name,
        "zone_id": result.state.zone_id or ob_state.zone_id,
    }

    logger.info(
        "[Onboarding] Step=%s completed=%s",
        result.state.step.value, result.state.completed,
    )
    return updates

__all__ = ["onboarding_node"]