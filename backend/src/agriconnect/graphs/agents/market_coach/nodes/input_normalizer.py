from typing import Any, Dict
from agriconnect.graphs.agents.market_coach.core.base import get_node_logger
from agriconnect.graphs.agents.market_coach.utils import MarketRuntime, _normalize_text, _now, INTENT_CONFIG
from agriconnect.graphs.agents.market_coach.services.profile_loader import (
    load_user_profile,
    preload_farms,
    _mask_phone,
)
from agriconnect.graphs.agents.market_coach.services.onboarding import resolve_onboarding_state
import copy
import inspect

logger = get_node_logger("InputNormalizer")


async def input_normalizer(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Normalise l'entrée utilisateur, charge le contexte profil et maintient le tunnel."""
    raw_text = state.get("user_query") or state.get("transcribed_audio") or ""
    turn = int(state.get("turn_count") or 0) + 1

    updates: Dict[str, Any] = {
        "timestamp": _now(),
        "turn_count": turn,
        "final_response": None,
        "ag_ui_component": None,
        "response_strategy": None,
    }

    # Audio transcription
    audio_path = state.get("audio_file_path")
    if audio_path and not state.get("transcribed_audio"):
        transcriber = getattr(mc_runtime, "transcribe_audio", None)
        if callable(transcriber):
            try:
                maybe_coro = transcriber(audio_path)
                transcribed = await maybe_coro if inspect.isawaitable(maybe_coro) else maybe_coro
                if transcribed:
                    updates["transcribed_audio"] = str(transcribed).strip()
                    raw_text = transcribed
            except Exception as audio_err:
                logger.error("[Normalizer] Échec de la transcription audio: %s", audio_err, exc_info=True)

    normalized = _normalize_text(raw_text)
    updates["normalized_text"] = normalized
    updates["translated_text"] = normalized
    updates["detected_language"] = "fr"

    # Phone extraction
    phone = state.get("phone") or state.get("user_phone") or state.get("phone_number")
    if not phone and isinstance(state.get("state_updates"), dict):
        phone = state.get("state_updates").get("phone")
    logger.info("[Normalizer] Téléphone extrait pour validation MCP : %s", _mask_phone(phone))

    if state.get("user_context_loaded") and phone:
        updates.setdefault("is_onboarding", False)
        updates.setdefault("onboarding_step", "COMPLETED")
        updates.setdefault("onboarding_internal_step", "__NONE__")
        logger.info("[Normalizer] Profil déjà chargé — onboarding désactivé")

    elif state.get("is_onboarding") and phone:
        # Orchestrator already detected a new user — skip redundant MCP call.
        tx_payload = dict(state.get("transaction_payload") or {})
        tx_payload["phone"] = str(phone).strip()
        current_step = (
            state.get("onboarding_internal_step")
            or state.get("onboarding_step")
            or "COLLECT_ROLE"
        )
        updates.update({
            "user_context_loaded": False,
            "is_onboarding": True,
            "onboarding_step": current_step,
            "onboarding_internal_step": current_step,
            "transaction_payload": tx_payload,
            "user_phone": str(phone).strip(),
        })
        logger.info("[Normalizer] Onboarding déjà activé par l'orchestrateur — skip MCP")

    elif not state.get("user_context_loaded") and phone:
        try:
            profile_updates = await load_user_profile(str(phone), mc_runtime)

            if profile_updates.get("user_context_loaded"):
                updates.update(profile_updates)
                updates["user_role"] = profile_updates.get("user_role") or state.get("user_role") or "PRODUCER"

                if state.get("user_farms_cache") is None:
                    try:
                        farm_list = await preload_farms(str(phone), mc_runtime)
                        updates["user_farms_cache"] = farm_list
                        logger.info("[Normalizer] Cache synchronisé : %d ferme(s).", len(farm_list))
                    except Exception as farm_exc:
                        logger.warning("[Normalizer] Échec non-fatal du préchargement des fermes: %s", farm_exc)
                        updates["user_farms_cache"] = []

            elif profile_updates.get("_new_user"):
                tx_payload = dict(state.get("transaction_payload") or {})
                if phone:
                    tx_payload["phone"] = str(phone).strip()

                current_step = (
                    state.get("onboarding_internal_step")
                    or state.get("onboarding_step")
                    or "COLLECT_ROLE"
                )
                updates.update({
                    "user_context_loaded": False,
                    "is_onboarding": True,
                    "onboarding_step": current_step,
                    "onboarding_internal_step": current_step,
                    "transaction_payload": tx_payload,
                    "user_phone": str(phone).strip() if phone else state.get("user_phone"),
                })
            else:
                updates["user_context_loaded"] = False

        except Exception as db_err:
            logger.error("[Normalizer] Erreur de communication critique avec le serveur MCP DB: %s", db_err, exc_info=True)
            updates["user_context_loaded"] = False

    # Onboarding resolution
    resolve_onboarding_state(state, updates)

    # Tunnel context alignment
    current_goal = state.get("current_goal")
    if current_goal and current_goal != "DISAMBIGUATION_PENDING":
        goal_config = INTENT_CONFIG.get(current_goal) or {}
        goal_label = goal_config.get("label", current_goal)

        wm = copy.deepcopy(state.get("working_memory") or {})
        wm["active_tunnel_label"] = goal_label
        wm["turn_count"] = turn
        updates["working_memory"] = wm

    return updates
