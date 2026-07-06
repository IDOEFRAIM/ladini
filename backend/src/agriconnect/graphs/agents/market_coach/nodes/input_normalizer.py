from typing import Any, Dict
from agriconnect.graphs.agents.market_coach.core.base import get_node_logger
from agriconnect.graphs.agents.market_coach.utils import MarketRuntime, ensure_dict, _normalize_text,_now,INTENT_CONFIG
import copy
import inspect

logger = get_node_logger("InputNormalizer")



async def input_normalizer(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Normalise l'entrée utilisateur, charge de manière sécurisée le contexte profil,
    précharge le cache des exploitations et maintient le tunnel conversationnel.
    
    Garantit l'immuabilité du state LangGraph via des copies profondes (deepcopy).
    """
    # 1. Extraction sécurisée et incrémentation du tour de parole
    raw_text = state.get("user_query") or state.get("transcribed_audio") or ""
    turn = int(state.get("turn_count") or 0) + 1
    
    # Initialisation du dictionnaire d'updates isolé
    # Purge render/cache fields at the beginning of each user turn.
    # Otherwise a stale final_response/UI component from turn N can be re-used on turn N+1.
    updates: Dict[str, Any] = {
        "timestamp": _now(),
        "turn_count": turn,
        "final_response": None,
        "ag_ui_component": None,
        "response_strategy": None,
    }

    # 2. Gestion de la transcription audio
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
                logger.error(f"[Normalizer] Échec de la transcription pour l'audio {audio_path}: {str(audio_err)}", exc_info=True)

    # Normalisation du texte d'entrée
    normalized = _normalize_text(raw_text)
    updates["normalized_text"] = normalized
    updates["translated_text"] = normalized
    updates["detected_language"] = "fr"

    # 3. 🛡️ FIX CRUCIAL : Extraction sécurisée du numéro de téléphone
    # On regarde d'abord dans 'phone', puis 'user_phone', et en dernier recours dans les métadonnées
    phone = state.get("phone") or state.get("user_phone") or state.get("phone_number")
    
    # Si c'était stocké par erreur dans state_updates, on essaie de le déballer s'il s'agit d'un dict
    if not phone and isinstance(state.get("state_updates"), dict):
        phone = state.get("state_updates").get("phone")
        
    logger.info(f"[Normalizer] Téléphone extrait pour validation MCP : {phone}")

    # When profile is already loaded from a previous turn, ensure onboarding
    # flags are explicitly cleared so stale metadata never re-triggers onboarding.
    if state.get("user_context_loaded") and phone:
        updates.setdefault("is_onboarding", False)
        updates.setdefault("onboarding_step", "COMPLETED")
        updates.setdefault("onboarding_internal_step", "__NONE__")
        logger.info("[Normalizer] Profil déjà chargé — onboarding désactivé")

    if not state.get("user_context_loaded") and phone:
        try:
            # Appel au serveur MCP de base de données avec le vrai numéro propre
            # Appel au serveur MCP de base de données
            profile_raw = await mc_runtime.call_db("get_user_by_phone", phone=str(phone).strip())
            res_dict = ensure_dict(profile_raw)
            logger.debug("[Normalizer] get_user_by_phone status=%s", (res_dict or {}).get("status"))

            # --- CORRECTION DU NORMALIZER ---
            # Utiliser .upper() ou comparer avec "SUCCESS" pour être cohérent avec votre méthode pivot
            status = str(res_dict.get("status", "")).upper()

            if status == "SUCCESS" and "data" in res_dict:
                profile = res_dict["data"] or {}
                updates["user_name"] = profile.get("name") or "N/A"
                updates["zone_name"] = profile.get("zone", {}).get("name")
                updates["zone_id"] = profile.get("zone", {}).get("id")
                # Correction importante : récupérer le rôle depuis le champ 'role' retourné par _serialize_user_entities
                updates["user_role"] = profile.get("role") or state.get("user_role") or "PRODUCER"
                updates["user_context_loaded"] = True
                updates["is_onboarding"] = False
                updates["onboarding_step"] = None
                updates["onboarding_internal_step"] = None
                
                # Votre méthode de sérialisation renvoie "id" directement
                user_uuid = profile.get("id")
                if user_uuid:
                    updates["user_id"] = str(user_uuid)

                # Préchargement proactif
                if state.get("user_farms_cache") is None:
                    try:
                        # Assurez-vous que l'outil 'get_producer_farm' accepte bien le format de 'phone' 
                        # utilisé dans _fetch_user_entities
                        farms_raw = await mc_runtime.call_db("get_producer_farm", phone=str(phone).strip())
                        farms_data = ensure_dict(farms_raw)
                        farm_list = farms_data.get("data") or farms_data.get("farms") or []
                        
                        updates["user_farms_cache"] = farm_list if isinstance(farm_list, list) else []
                        logger.info(f"[Normalizer] Cache synchronisé : {len(updates['user_farms_cache'])} ferme(s).")
                    except Exception as farm_exc:
                        logger.warning(f"[Normalizer] Échec non-fatal du préchargement des fermes: {str(farm_exc)}")
                        updates["user_farms_cache"] = []
            elif status == "NEW_USER":
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
                # Si on arrive ici, soit status n'est pas "SUCCESS", soit le profil est réellement absent
                logger.warning(f"[Normalizer] Profil introuvable ou erreur status ({status}) pour {phone}")
                updates["user_context_loaded"] = False
                
        except Exception as db_err:
            logger.error(f"[Normalizer] Erreur de communication critique avec le serveur MCP DB: {str(db_err)}", exc_info=True)
            updates["user_context_loaded"] = False

    # --- Onboarding activation check ---
    # Use a sentinel to distinguish "explicitly set to None" from "not set".
    _SENTINEL = object()
    raw_internal = updates.get("onboarding_internal_step", _SENTINEL)
    internal_step = raw_internal if raw_internal is not _SENTINEL else state.get("onboarding_internal_step")
    # Treat our own sentinel value "__NONE__" as cleared
    if internal_step == "__NONE__":
        internal_step = None

    raw_display = updates.get("onboarding_step", _SENTINEL)
    display_step = raw_display if raw_display is not _SENTINEL else state.get("onboarding_step")
    display_label = str(display_step or "").upper()

    step_active = bool(internal_step) or display_label not in {"", "COMPLETED"}

    # is_onboarding explicitly set in updates takes absolute priority
    explicit_onboarding = updates.get("is_onboarding", _SENTINEL)
    if explicit_onboarding is not _SENTINEL:
        onboarding_active = bool(explicit_onboarding)
    else:
        onboarding_active = bool(state.get("is_onboarding") or step_active)

    if onboarding_active:
        updates["is_onboarding"] = True
        updates["interpreted_event"] = "ONBOARDING_INPUT"
        updates["detected_intent"] = "ONBOARDING"
        updates["interpreter_confidence"] = 1.0
        updates.setdefault("status", "WAITING_INPUT")
    else:
        updates["is_onboarding"] = False

    # 4. Alignement du contexte de tunnel transactionnel (Immuabilité préservée)
    current_goal = state.get("current_goal")
    if current_goal and current_goal != "DISAMBIGUATION_PENDING":
        goal_config = INTENT_CONFIG.get(current_goal) or {}
        goal_label = goal_config.get("label", current_goal)
        
        wm = copy.deepcopy(state.get("working_memory") or {})
        wm["active_tunnel_label"] = goal_label
        wm["turn_count"] = turn
        updates["working_memory"] = wm

    return updates

