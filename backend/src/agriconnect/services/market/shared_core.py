"""Market — Shared Core (helpers, constants, fast-paths, universal nodes).

Module commun aux deux agents Market (Producer & Buyer). Contient :
  - les constantes vocabulaires WhatsApp,
  - les helpers de normalisation et de sécurité,
  - la résolution générique des index courts → UUID via `available_mapping`,
  - les huit nœuds universels du graphe (les nœuds spécifiques au rôle vivent
    dans `producer_flow.py` ou `buyer_flow.py`).

Tous les nœuds restent strictement déterministes hors interpréteur LLM (Node 3).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import copy
import re
import time
import unicodedata
from typing import Any, Awaitable, Callable, Dict, List, Optional

from agriconnect.graphs.agents.market_coach.actions import (
    MARKET_READ_ACTIONS_MAP,
    MARKET_WRITE_ACTIONS_MAP,
)
from agriconnect.graphs.agents.market_coach.intent import (
    INTENT_CONFIG,
    INTENT_DISAMBIGUATION,
)
from agriconnect.graphs.agents.market_coach.state import MarketAgentState
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    ensure_dict,
    is_success_response,
)

# Refactor: extracted response composition + routing to satellite modules
from .intent_router import response_strategy
from .response_handlers import final_response, _label_for_field

logger = logging.getLogger("AgriConnect.Market.SharedCore")

_WRITE_GOALS = frozenset(MARKET_WRITE_ACTIONS_MAP.keys())
_READ_GOALS = frozenset(MARKET_READ_ACTIONS_MAP.keys())

def _clean_candidate_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip(" '\"\n\r\t")
    return cleaned or None


async def _llm_extract_onboarding_field(
    mc_runtime: MarketRuntime,
    field: str,
    user_text: str,
) -> Optional[str]:
    """Utilise le LLM pour extraire de manière robuste un champ d'onboarding (name, zone, role)."""
    if not user_text:
        return None
    llm = getattr(mc_runtime, "llm", None)
    if llm is None:
        return None

    # Définition des instructions par champ
    instructions = {
        "name": "Si le message contient un nom complet ou un prénom, retourne-le. Sinon retourne vide.",
        "zone": "Si le message mentionne une ville/zone/province, retourne le nom propre. Sinon retourne vide.",
        "role": "Analyse l'intention de l'utilisateur. Retourne 'PRODUCER' s'il veut vendre ou cultiver, ou 'BUYER' s'il veut acheter. Retourne 'PRODUCER' par défaut si ambigu.",
    }
    
    # Validation du champ
    field_key = field if field in instructions else "name"
    
    system_prompt = (
        "Tu extrais des informations d'onboarding pour AgriConnect. "
        "Réponds uniquement un JSON {\"value\": \"...\"}. "
        f"Instruction: {instructions.get(field_key)}"
    )

    try:
        completion = await asyncio.to_thread(
            lambda: llm.chat.completions.create(
                model=getattr(mc_runtime, "model_answer", "llama-3.3-70b-versatile"),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=60,
            )
        )
        payload = json.loads(completion.choices[0].message.content or "{}")
        value = payload.get("value")
        
        # Nettoyage spécifique pour le rôle afin de garantir les valeurs attendues par la BDD
        if field_key == "role":
            val = str(value).upper()
            return "BUYER" if "BUY" in val else "PRODUCER"
            
        return _clean_candidate_text(value)
        
    except Exception as exc:
        logger.debug("[Onboarding] LLM extraction failed for %s: %s", field, exc)
        return "PRODUCER" if field == "role" else None



def _now() -> float:
    return time.time()

def _normalize_text(raw: str) -> str:
    if not raw:
        return ""
    return re.sub(r"\s+", " ", raw).strip()

def _normalize_quantity_to_kg(payload: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(payload or {})
    qty = normalized.get("quantity_mentioned")
    unit = str(normalized.get("unit_mentioned") or "").strip().upper()
    if qty in (None, "", [], {}) or not unit:
        return normalized
    try:
        qf = float(qty)
    except (TypeError, ValueError):
        return normalized
    if unit in {"TONNE", "TONNES", "T"}:
        normalized["quantity_mentioned"] = qf * 1000.0
        normalized["unit_mentioned"] = "KG"
        normalized["unit_conversion"] = {"from_unit": unit, "to_unit": "KG", "factor": 1000}
    elif unit in {"KG", "KILO", "KILOS", "KILOGRAMME", "KILOGRAMMES"}:
        normalized["quantity_mentioned"] = qf
        normalized["unit_mentioned"] = "KG"
    return normalized

def _safe_node(
    fn: Callable[[Dict[str, Any], MarketRuntime], Awaitable[Dict[str, Any]]],
    name: str,
) -> Callable[..., Awaitable[Dict[str, Any]]]:
    async def _wrapped(state: Dict[str, Any], mc_runtime: MarketRuntime, **_: Any) -> Dict[str, Any]:
        try:
            result = await fn(state, mc_runtime)
            if not isinstance(result, dict):
                return {
                    "status": "ERROR",
                    "validation_errors": [f"Node {name} returned invalid payload"],
                }
            return result
        except Exception as exc:
            logger.exception("[%s] failed: %s", name, exc)
            return {
                "status": "ERROR",
                "validation_errors": [f"Node {name} failed: {exc}"],
            }
    return _wrapped

def _maybe_await(obj: Any) -> Any:
    if inspect.isawaitable(obj) or asyncio.iscoroutine(obj):
        return obj
    return None

def reset_error_status(state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "PLANNING",
        "validation_errors": [],
        "execution_authorized": False,
        "is_certified": False,
        "waiting_for_confirmation": False,
        "retry_count": 0,
    }


_GENERIC_TECHNICAL_ERROR = (
    "Une erreur technique est survenue lors de l'enregistrement. "
    "Veuillez réessayer ultérieurement."
)


# =====================================================================
# NODE 5 — INPUT NORMALIZER
# =====================================================================



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

                current_step = state.get("onboarding_step") or "COLLECT_NAME"
                updates.update({
                    "user_context_loaded": False,
                    "is_onboarding": True,
                    "onboarding_step": current_step,
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

    onboarding_active = bool(
        updates.get("is_onboarding")
        or state.get("is_onboarding")
        or state.get("onboarding_step")
    )
    if onboarding_active:
        updates["interpreted_event"] = "ONBOARDING_INPUT"
        updates["detected_intent"] = "ONBOARDING"
        updates["interpreter_confidence"] = 1.0
        updates.setdefault("status", "WAITING_INPUT")

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


# =====================================================================
# NODE 6 — SECURITY MODERATION
# =====================================================================

async def security_moderation(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Vérifie si l'entrée utilisateur contient des risques de sécurité ou des fraudes."""
    text = state.get("normalized_text") or state.get("user_query") or ""
    if not text:
        return {"security_status": "SAFE", "trust_score": 1.0, "ag_ui_component": None}

    security = getattr(mc_runtime, "security", None)
    if security is None or not hasattr(security, "moderate_content"):
        return {"security_status": "SAFE", "trust_score": 0.8, "ag_ui_component": None}

    raw = security.moderate_content(text)
    if inspect.isawaitable(raw) or asyncio.iscoroutine(raw):
        raw = await raw

    if not isinstance(raw, dict):
        return {"security_status": "SAFE", "trust_score": 0.5, "ag_ui_component": None}

    status_raw = str(raw.get("status") or "").upper()
    is_scam = bool(raw.get("is_scam"))
    if is_scam or status_raw in {"SCAM_DETECTED", "BLOCKED"}:
        return {
            "security_status": "SCAM_DETECTED",
            "security_reason": raw.get("reason") or "Contenu suspect détecté.",
            "trust_score": 0.0,
            "status": "BLOCKED",
            "response_strategy": "ERROR",
            "final_response": (
                "Désolé, votre message contient des éléments suspects. "
                "Pour votre sécurité, je ne peux pas continuer cette opération."
            ),
            "ag_ui_component": {
                "lc_type": "constructor",
                "id": ["ag_ui", "StatusComponent"],
                "kwargs": {"type": "error", "reason": "Contenu suspect détecté"},
            },
        }

    return {"security_status": "SAFE", "trust_score": 1.0, "ag_ui_component": None}


# =====================================================================
# NODE 6A — SEMANTIC DISAMBIGUATION
# =====================================================================
# S'intercale après input_interpreter quand le LLM est peu confiant ET que
# le texte contient un déclencheur lexical présent dans `INTENT_DISAMBIGUATION`.
# Si déclenchée, propose un AG-UI ListMenu et court-circuite goal_planner pour
# l'envoyer directement vers response_strategy. La sélection N de l'utilisateur
# au tour suivant est résolue par memory_update via mapping_kind="intent_disambiguation",
# puis exploitée par goal_planner (RÈGLE 0bis) comme un NEW_TASK propre.

# Seuil de confiance LLM en-dessous duquel on autorise la désambiguïsation.
_DISAMBIGUATION_CONFIDENCE_THRESHOLD = 0.85
_RECOVERY_MAX_RETRIES = 2


def _detect_disambiguation_candidates(text_lower: str) -> Optional[Dict[str, Any]]:
    """Cherche dans INTENT_DISAMBIGUATION une entrée dont les `lexical_hints` matchent.

    INTENT_DISAMBIGUATION est un dict {trigger_key: {candidates, title, options, lexical_hints}}.
    Retourne (key, entry) la première paire matchée, ou None.
    """
    for key, entry in INTENT_DISAMBIGUATION.items():
        hints = entry.get("lexical_hints") or []
        for hint in hints:
            if hint and str(hint).lower() in text_lower:
                return {"id": key, **entry}
    return None


def _compute_progress(goal: Optional[str], payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Calcule la progression de remplissage du formulaire pour le but courant."""
    if not goal:
        return None
    config = INTENT_CONFIG.get(goal) or {}
    required = list(config.get("required") or [])
    if not required:
        return None
    filled = [f for f in required if f not in _AUTO_RESOLVABLE_FIELDS and payload.get(f) not in (None, "", [], {})]
    return {
        "total": len([f for f in required if f not in _AUTO_RESOLVABLE_FIELDS]),
        "filled": len(filled),
        "remaining": [f for f in required if f not in _AUTO_RESOLVABLE_FIELDS and f not in [x for x in filled]],
        "pct": round(len(filled) / max(len(required), 1) * 100),
    }


def _build_proactive_hint(goal: Optional[str], progress: Optional[Dict[str, Any]], payload: Dict[str, Any]) -> Optional[str]:
    """Génère un indice proactif contextuel pour guider l'utilisateur."""
    if not goal or not progress:
        return None
    pct = progress.get("pct", 0)
    remaining = progress.get("remaining") or []
    goal_label = (INTENT_CONFIG.get(goal) or {}).get("label", goal)

    if pct == 0:
        return f"Nouvelle opération : {goal_label}"
    if pct >= 100:
        return "Toutes les informations sont réunies, prêt pour confirmation."
    if len(remaining) == 1:
        field_label = _label_for_field(goal, remaining[0])
        return f"Plus qu'une info : {field_label}."
    return f"Progression : {pct}% — encore {len(remaining)} infos nécessaires."


async def cognitive_guard(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    """Couche cognitive bornée : détecte interruptions, gère la récupération,
    enrichit les entités depuis le contexte stable, calcule la progression,
    et injecte des indices proactifs pour le coaching conversationnel."""
    event = str(state.get("interpreted_event") or "").upper()
    detected_intent = str(state.get("detected_intent") or "UNKNOWN").upper()
    confidence = float(state.get("interpreter_confidence") or 0.0)
    expected_input = str(state.get("expected_input") or "NONE").upper()
    current_goal = state.get("current_goal")
    payload = dict(state.get("transaction_payload") or {})
    text_lower = (state.get("normalized_text") or state.get("user_query") or "").lower()
    retry_count = int(state.get("retry_count") or 0)
    in_tunnel = bool(current_goal and expected_input not in {"NONE", ""})

    updates: Dict[str, Any] = {}

    decision: Dict[str, Any] = {
        "event": event,
        "intent": detected_intent,
        "confidence": confidence,
        "expected_input": expected_input,
        "in_tunnel": in_tunnel,
        "bounded": True,
    }
    competition: List[Dict[str, Any]] = []

    # --- INTENT COMPETITION SCORING ---
    entry = _detect_disambiguation_candidates(text_lower)
    if entry:
        for intent_key in entry.get("candidates") or []:
            competition.append({
                "intent": intent_key,
                "source": "lexical_disambiguation",
                "trigger": entry.get("id"),
            })
    if detected_intent != "UNKNOWN":
        competition.append({
            "intent": detected_intent,
            "source": "llm_interpreter",
            "confidence": confidence,
        })

    # --- ENTITY CARRY-FORWARD (Stable Context) ---
    # Pendant un tunnel actif, si l'utilisateur ne re-spécifie pas un champ,
    # on hérite de stable_entities (ex: produit sticks pendant le slot-filling)
    if in_tunnel and current_goal:
        stable = state.get("stable_entities") or {}
        entities = dict(state.get("extracted_entities") or {})
        carried = False
        for key in ("product", "unit_mentioned", "zone_name"):
            if not entities.get(key) and stable.get(key):
                entities[key] = stable[key]
                carried = True
        if carried:
            updates["extracted_entities"] = entities
            decision["entity_carry_forward"] = True

    # --- SMART INTERRUPTION DETECTION ---
    # Uniquement si l'utilisateur lance réellement un NOUVEAU sujet différent
    if current_goal and event == "NEW_TASK" and detected_intent not in {"UNKNOWN", str(current_goal).upper()}:
        updates.update({
            "interpreted_event": "INTERRUPTION",
            "intent_competition": competition,
            "cognitive_decision": {**decision, "action": "suspend_current_goal"},
        })
        return updates

    # --- BOUNDED RECOVERY FOR ACTIVE TUNNEL ---
    # Si l'utilisateur dévie pendant un slot-filling, on le ramène poliment
    if event == "UNKNOWN" and in_tunnel:
        if retry_count >= _RECOVERY_MAX_RETRIES:
            # Escalade : abandonner le tunnel après trop de tentatives
            logger.warning(
                "[CognitiveGuard] Max retries (%d) reached for goal=%s — abandoning tunnel",
                _RECOVERY_MAX_RETRIES, current_goal,
            )
            updates.update({
                "current_goal": None,
                "goal_status": "IDLE",
                "status": "WAITING_INPUT",
                "transaction_payload": {},
                "stable_entities": {},
                "missing_fields": [],
                "completed_fields": [],
                "last_missing_field": None,
                "expected_input": "NONE",
                "expected_candidates": [],
                "available_mapping": {},
                "retry_count": 0,
                "waiting_for_confirmation": False,
                "confirmation_summary": None,
                "selected_tool": None,
                "selected_tool_args": {},
                "execution_result": {},
                "ag_ui_component": None,
                "response_strategy": "CLARIFICATION",
                "intent_competition": competition,
                "cognitive_decision": {**decision, "action": "abandon_tunnel_max_retries"},
                "proactive_hint": "L'opération a été annulée. Dites-moi ce que vous souhaitez faire.",
            })
            return updates

        updates.update({
            "status": "WAITING_INPUT",
            "current_goal": current_goal,
            "goal_status": "WAITING_INPUT",
            "response_strategy": "RECOVERY",
            "intent_competition": competition,
            "cognitive_decision": {**decision, "action": "recover_active_tunnel", "retry": retry_count},
        })
        return updates

    # --- EXPRESS MODE DETECTION ---
    # If user provides rich entities (multiple fields at once during tunnel),
    # detect potential completion and signal fast-track to confirmation
    entities = state.get("extracted_entities") or {}
    entity_count = sum(1 for v in entities.values() if v not in (None, "", [], {}))
    if in_tunnel and entity_count >= 2 and event == "ANSWER":
        decision["express_mode"] = True
        decision["entity_richness"] = entity_count

    # --- PROGRESS COMPUTATION + COACHING HINT ---
    # Merge entities into payload snapshot for progress calc
    progress_payload = {**payload}
    for k, v in entities.items():
        if v not in (None, "", [], {}):
            progress_payload[k] = v
    progress = _compute_progress(current_goal, progress_payload)
    if progress:
        updates["conversation_progress"] = progress
        hint = _build_proactive_hint(current_goal, progress, progress_payload)
        if hint:
            updates["proactive_hint"] = hint

    updates.update({
        "intent_competition": competition,
        "cognitive_decision": {**decision, "action": "continue"},
    })
    return updates


async def cognitive_orchestrator(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    if state.get("is_onboarding"):
        event = str(state.get("interpreted_event") or "").upper()
        intent = str(state.get("detected_intent") or "UNKNOWN").upper()
        confidence = float(state.get("interpreter_confidence") or 0.0)
        return {
            "cognitive_decision": {
                "phase": "reason",
                "next_step": "onboarding",
                "reason": "onboarding",
                "loop": ["perceive", "think", "decide", "act", "observe", "reason", "retry"],
                "event": event,
                "intent": intent,
                "current_goal": None,
                "confidence": confidence,
            },
            "should_replan": False,
        }
    event = str(state.get("interpreted_event") or "").upper()
    intent = str(state.get("detected_intent") or "UNKNOWN").upper()
    current_goal = str(state.get("current_goal") or "").upper()
    expected_input = str(state.get("expected_input") or "NONE").upper()
    strategy = str(state.get("response_strategy") or "").upper()
    confidence = float(state.get("interpreter_confidence") or 0.0)
    competition = list(state.get("intent_competition") or [])
    in_tunnel = bool(current_goal and expected_input not in {"", "NONE"})

    phase = "perceive"
    next_step = "continue"
    reason = "nominal"

    if strategy in {"CLARIFICATION", "RECOVERY"}:
        phase = "reason"
        next_step = "respond"
        reason = strategy.lower()
    elif event == "INTERRUPTION":
        phase = "decide"
        next_step = "replan"
        reason = "interruption"
    elif competition and len({str(c.get("intent")) for c in competition if c.get("intent")}) > 1 and confidence < _DISAMBIGUATION_CONFIDENCE_THRESHOLD:
        phase = "think"
        next_step = "clarify"
        reason = "intent_competition"
    elif in_tunnel and event in {"ANSWER", "UPDATE", "SELECTION", "CONFIRM", "REJECT"}:
        phase = "act"
        next_step = "continue_tunnel"
        reason = "active_goal"
    elif intent == "UNKNOWN" and event in {"UNKNOWN", "OUT_OF_SCOPE"}:
        phase = "reason"
        next_step = "clarify"
        reason = "unknown_intent"

    return {
        "cognitive_decision": {
            "phase": phase,
            "next_step": next_step,
            "reason": reason,
            "loop": ["perceive", "think", "decide", "act", "observe", "reason", "retry"],
            "event": event,
            "intent": intent,
            "current_goal": current_goal or None,
            "confidence": confidence,
        },
        "should_replan": next_step == "replan",
    }


async def semantic_disambiguation(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    """Pose une question à choix multiples quand l'intention détectée est ambiguë.

    Active uniquement si TOUS les critères sont réunis :
      1. l'événement interprété est `NEW_TASK` (ou `UNKNOWN` avec entités),
      2. la confiance LLM est < seuil,
      3. le texte normalisé contient un déclencheur d'`INTENT_DISAMBIGUATION`,
      4. aucun tunnel de slot-filling actif (`expected_input == NONE`).

    Sinon : no-op pass-through (retourne updates vides).
    """
    event = str(state.get("interpreted_event") or "").upper()
    confidence = float(state.get("interpreter_confidence") or 1.0)
    expected_input = str(state.get("expected_input") or "NONE").upper()
    text_lower = (state.get("normalized_text") or state.get("user_query") or "").lower()

    # Pré-conditions strictes — sinon on laisse le flow nominal continuer
    if event not in {"NEW_TASK", "UNKNOWN"}:
        return {}
    if expected_input != "NONE":
        return {}
    if not text_lower:
        return {}

    entry = _detect_disambiguation_candidates(text_lower)
    if not entry:
        return {}
    if confidence >= _DISAMBIGUATION_CONFIDENCE_THRESHOLD and len(entry.get("candidates") or []) < 2:
        return {}

    # `options` est une liste de tuples (intent_key, label).
    options = entry.get("options") or []
    if len(options) < 2:
        return {}

    title = entry.get("title") or "Que souhaitez-vous faire exactement ?"
    pedagogical_intro = entry.get("pedagogical_hint") or ""
    mapping: Dict[str, str] = {}
    labels: List[str] = []
    descriptions: List[str] = []
    lines = [f"🤔 *{title}*"]
    if pedagogical_intro:
        lines.append(f"\n{pedagogical_intro}\n")
    for i, opt in enumerate(options, start=1):
        # Tolère soit (intent, label), soit dict-like {"intent": ..., "label": ...}
        if isinstance(opt, (tuple, list)) and len(opt) >= 2:
            intent_key, label = opt[0], opt[1]
        elif isinstance(opt, dict):
            intent_key, label = opt.get("intent"), opt.get("label")
        else:
            continue
        if not intent_key:
            continue
        mapping[str(i)] = str(intent_key)
        labels.append(str(label or intent_key))
        # Add business context from INTENT_CONFIG
        intent_label = (INTENT_CONFIG.get(str(intent_key)) or {}).get("label", "")
        desc = intent_label if intent_label != label else ""
        descriptions.append(desc)
        if desc:
            lines.append(f"{i}. *{label or intent_key}* \u2014 {desc}")
        else:
            lines.append(f"{i}. {label or intent_key}")

    if len(mapping) < 2:
        return {}

    # Add coaching suffix
    lines.append("\nRépondez simplement par le numéro de votre choix.")

    logger.info(
        "[Disambiguation] event=%s conf=%.2f trigger=%s candidates=%d",
        event, confidence, entry.get("id"), len(mapping),
    )

    return {
        "status": "WAITING_INPUT",
        "current_goal": "DISAMBIGUATION_PENDING",
        "goal_status": "WAITING_INPUT",
        "expected_input": "SELECTION",
        "expected_candidates": labels,
        "available_mapping": mapping,
        "working_memory": {
            **(state.get("working_memory") or {}),
            "available_mapping_kind": "intent_disambiguation",
            "disambiguation_pending": True,
            "disambiguation_trigger_id": entry.get("id"),
        },
        "response_strategy": "SELECTION_MENU",
        "final_response": "\n".join(lines),
        "ag_ui_component": {
            "lc_type": "constructor",
            "id": ["ag_ui", "ListMenu"],
            "kwargs": {
                "title": title,
                "options": [{"index": str(i), "label": lbl} for i, lbl in enumerate(labels, start=1)],
                "metadata": {"kind": "intent_disambiguation"},
            },
        },
    }


# =====================================================================
# NODE 6A-bis — CLARIFICATION NODE (Pedagogical Guidance)
# =====================================================================

async def clarification_node(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Noeud de clarification pédagogique.

    Activé quand :
    - L'événement est OUT_OF_SCOPE ou UNKNOWN sans tunnel actif
    - Le cognitive_guard a décidé d'abandonner un tunnel

    Utilise le LLM pour générer une réponse contextualisée, chaleureuse
    et pédagogique plutôt qu'un message d'erreur froid.
    """
    event = str(state.get("interpreted_event") or "").upper()
    current_goal = state.get("current_goal")
    expected_input = str(state.get("expected_input") or "NONE").upper()
    cognitive = state.get("cognitive_decision") or {}
    cognitive_action = cognitive.get("action", "")
    user_role = str(state.get("user_role") or "PRODUCER").upper()
    user_name = state.get("user_name") or ""
    text = state.get("normalized_text") or state.get("user_query") or ""

    # Only intervene on specific conditions
    needs_clarification = (
        (event in {"OUT_OF_SCOPE", "UNKNOWN"} and expected_input == "NONE" and not current_goal)
        or cognitive_action == "abandon_tunnel_max_retries"
    )
    if not needs_clarification:
        return {}
    if _detect_disambiguation_candidates(text.lower()):
        return {}

    # Try LLM-powered clarification
    llm = getattr(mc_runtime, "llm", None)
    if llm is None:
        return {}  # fallback handled by final_response CLARIFICATION

    if user_role == "BUYER":
        capabilities = (
            "chercher des produits agricoles, lancer un appel d'offres, "
            "voir les offres en cours, ou suivre vos commandes"
        )
    else:
        capabilities = (
            "enregistrer une récolte, mettre en vente un produit, "
            "gérer votre stock, ou répondre aux demandes d'acheteurs"
        )

    context_parts = []
    if cognitive_action == "abandon_tunnel_max_retries":
        context_parts.append("L'opération précédente a été annulée car je n'arrivais pas à comprendre.")
    if text:
        context_parts.append(f"L'utilisateur a dit : \"{text}\"")

    prompt = (
        f"Tu es un assistant commercial agricole WhatsApp au Burkina Faso.\n"
        f"Ton style : coach amical, encourageant, patient.\n"
        f"L'utilisateur ({user_name or 'un producteur'}, rôle {user_role}) "
        f"a envoyé un message que tu ne comprends pas.\n"
        f"{'  '.join(context_parts)}\n\n"
        f"Tu peux l'aider à : {capabilities}.\n"
        f"Explique brièvement ce que tu peux faire et encourage-le à reformuler.\n"
        f"Donne 2-3 exemples concrets de phrases qu'il pourrait dire.\n"
        f"Réponds en 2-3 phrases max, en français simple et direct."
    )

    try:
        completion = await asyncio.to_thread(
            lambda: llm.chat.completions.create(
                model=getattr(mc_runtime, "model_answer", "llama-3.3-70b-versatile"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=150,
            )
        )
        result = (completion.choices[0].message.content or "").strip()
        if result:
            return {
                "final_response": result,
                "response_strategy": "CLARIFICATION",
                "ag_ui_component": None,
            }
    except Exception as exc:
        logger.warning("[ClarificationNode] LLM call failed: %s", exc)

    return {}


# =====================================================================
# NODE 6A-ter — ONBOARDING NODE
# =====================================================================


async def onboarding_node(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Collecte les informations essentielles (nom, zone, rôle) pour les nouveaux utilisateurs."""
    
    in_onboarding = bool(state.get("is_onboarding"))
    existing_strategy = str(state.get("response_strategy") or "").upper()
    if not in_onboarding and existing_strategy != "ONBOARDING":
        logger.info("[Onboarding] Sortie immédiate : conditions d'onboarding non remplies.")
        return {}

    

    payload = dict(state.get("transaction_payload") or {})
    extracted = state.get("extracted_entities") or {}
    normalized_query = (state.get("normalized_text") or state.get("user_query") or "").strip()
    
    # 🛡️ Consolidation du numéro de téléphone
    phone = payload.get("phone") or state.get("user_phone") or state.get("phone") or state.get("phone_number")
    if phone:
        payload["phone"] = str(phone).strip()

    step = state.get("onboarding_step") or "COLLECT_NAME"
    logger.info("[Onboarding] ---> ENTRÉE DANS ONBOARDING_NODE | Étape actuelle: %s", step)
    prompt: str = ""
    updates: Dict[str, Any] = {
        "response_strategy": "ONBOARDING",
        "status": "WAITING_INPUT",
        "ag_ui_component": None,
        "onboarding_step": step,
    }

    # Fonction utilitaire pour garantir la persistance du payload à chaque étape
    def _sync_payload() -> None:
        updates["transaction_payload"] = dict(payload)

    _sync_payload()

    # 🛡️ NOUVEAU : Extraction proactive du rôle dès que possible
    if not payload.get("role") and normalized_query:
        extracted_role = await _llm_extract_onboarding_field(mc_runtime, "role", normalized_query)
        if extracted_role:
            payload["role"] = extracted_role
            _sync_payload()
            logger.debug("[Onboarding] Rôle extrait via LLM: %s", extracted_role)

    # ==========================================
    # ETAPE 1 : COLLECTE DU NOM
    # ==========================================
    if step == "COLLECT_NAME":
        candidate_name = payload.get("name") or extracted.get("name")
        
        # Secours via LLM si l'entité classique a échoué
        if not candidate_name and normalized_query:
            candidate_name = await _llm_extract_onboarding_field(mc_runtime, "name", normalized_query)
            if candidate_name:
                logger.debug("[Onboarding] Nom extrait via LLM: %s", candidate_name)
        
        if candidate_name:
            payload["name"] = str(candidate_name).strip()
            _sync_payload()
            step = "COLLECT_ZONE"
            updates["onboarding_step"] = step
            prompt = (
                f"Enchanté {payload['name']} ! Dans quelle zone travaillez-vous ? "
                "Vous pouvez répondre par une ville comme Ouagadougou, Bobo Dioulasso, Banfora ou Ziniaré."
            )
        else:
            prompt = "Bienvenue sur AgriConnect ! Quel est votre nom complet ?"

    # ==========================================
    # ETAPE 2 : COLLECTE DE LA ZONE & CREATION
    # ==========================================
    elif step == "COLLECT_ZONE":
        previous_zone_name = str(payload.get("zone_name") or "").strip()
        zone_name = (
            extracted.get("zone_name")
            or extracted.get("location")
            or extracted.get("zone")
            or payload.get("zone_name")
        )
        
        # Secours via LLM si l'entité classique a échoué
        if not zone_name and normalized_query:
            zone_name = await _llm_extract_onboarding_field(mc_runtime, "zone", normalized_query)
            if zone_name:
                logger.debug("[Onboarding] Zone extraite via LLM: %s", zone_name)

        if zone_name:
            normalized_zone = str(zone_name).strip()
            if (
                previous_zone_name
                and normalized_zone
                and normalized_zone.lower() != previous_zone_name.lower()
                and payload.pop("zone_id", None) is not None
            ):
                logger.debug(
                    "[Onboarding] Zone modifiée (%s → %s), remise à zéro du zone_id",
                    previous_zone_name,
                    normalized_zone,
                )
            payload["zone_name"] = normalized_zone
            _sync_payload()

        zone_id = payload.get("zone_id") or extracted.get("zone_id")
        if zone_id:
            payload["zone_id"] = str(zone_id).strip()
            _sync_payload()

        # Validation de la zone auprès de la base de données
        if payload.get("zone_name") and not payload.get("zone_id"):
            try:
                res_zone = await mc_runtime.call_db("get_zone_by_name", name=payload["zone_name"])
                zone_data = ensure_dict(res_zone)
            except Exception as zone_err:
                logger.error("[Onboarding] Échec de la recherche de zone: %s", zone_err)
                zone_data = {"status": "ERROR", "message": str(zone_err)}
                
            if str(zone_data.get("status", "")).upper() == "SUCCESS":
                zone_payload = zone_data.get("data") or {}
                if zone_payload.get("id"):
                    payload["zone_id"] = str(zone_payload["id"])
                if zone_payload.get("name"):
                    payload["zone_name"] = zone_payload["name"]
                _sync_payload()
            else:
                prompt = (
                    f"Je n'ai pas trouvé la zone '{payload['zone_name']}'. "
                    "Merci d'indiquer une ville ou province valide (ex : Ouagadougou, Bobo Dioulasso, Banfora). "
                    "Si vous hésitez, donnez la ville la plus proche ou contactez le support au +22601479800 pour ajouter votre zone."
                )
                updates.update({
                    "onboarding_prompt": prompt,
                    "status": "WAITING_INPUT",
                    "onboarding_step": "COLLECT_ZONE",
                })
                _sync_payload()
                return updates

        # 🛡️ VALIDATION FINALE & CRÉATION DU PROFIL
        if payload.get("zone_name") and payload.get("zone_id") and payload.get("name") and payload.get("phone"):
            create_payload = {
                "phone": payload["phone"],
                "name": payload["name"],
                "role": payload.get("role") or state.get("user_role") or "PRODUCER",
                "zone_id": payload["zone_id"],
            }

            try:
                result = await mc_runtime.call_db("create_user_profile", data=create_payload)
                res_dict = ensure_dict(result)
                
                if str(res_dict.get("status", "")).upper() == "SUCCESS":
                    prompt = "Merci ! Votre profil est créé. Que souhaitez-vous faire maintenant ?"
                    updates.update({
                        "is_onboarding": False,
                        "onboarding_step": None,
                        "transaction_payload": {},
                        "status": "PLANNING",
                        "user_phone": create_payload["phone"],
                    })

                    # Tentative de rechargement du profil complet en mémoire
                    try:
                        profile_raw = await mc_runtime.call_db("get_user_by_phone", phone=create_payload["phone"])
                        profile_dict = ensure_dict(profile_raw)
                        if str(profile_dict.get("status", "")).upper() == "SUCCESS":
                            profile = profile_dict.get("data") or {}
                            zone_meta = profile.get("zone") or {}
                            updates.update({
                                "user_context_loaded": True,
                                "user_name": profile.get("name") or create_payload["name"],
                                "user_role": profile.get("role") or create_payload["role"],
                                "user_id": str(profile.get("id")) if profile.get("id") else None,
                                "zone_name": zone_meta.get("name"),
                                "zone_id": zone_meta.get("id"),
                            })
                    except Exception as profile_err:  # pragma: no cover - log only
                        logger.warning("[Onboarding] Impossible de rafraîchir le profil: %s", profile_err)
                else:
                    prompt = "Impossible de créer votre profil pour le moment. Pouvez-vous confirmer votre zone ?"
                    updates["status"] = "ERROR"
                    updates["onboarding_step"] = "COLLECT_ZONE"
                    _sync_payload()
                    
            except Exception as exc:  # pragma: no cover - log only
                logger.error("[Onboarding] create_user_profile a échoué: %s", exc)
                prompt = "Une erreur technique est survenue. Merci de préciser à nouveau votre zone pour réessayer."
                updates["status"] = "ERROR"
                updates["onboarding_step"] = "COLLECT_ZONE"
                _sync_payload()
        else:
            prompt = "Dans quelle zone (ville ou province) opérez-vous ?"
            
    # ==========================================
    # FALLBACK
    # ==========================================
    else:
        step = "COLLECT_NAME"
        updates["onboarding_step"] = step
        prompt = "Bienvenue sur AgriConnect ! Quel est votre nom complet ?"

    # Assignation finale des champs
    if "onboarding_prompt" not in updates:
        updates["onboarding_prompt"] = prompt or "Merci de partager ces informations."
    if "transaction_payload" not in updates:
        updates["transaction_payload"] = dict(payload)
        
    return updates


# =====================================================================
# NODE 6B — MEMORY UPDATE
# =====================================================================

async def memory_update(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Met à jour la mémoire de transaction, résout les sélections AG-UI,
    hérite les entités stables pour continuité inter-tours, et injecte
    les défauts profil (zone) quand l'utilisateur ne les spécifie pas."""
    extracted: Dict[str, Any] = state.get("extracted_entities") or {}
    payload: Dict[str, Any] = dict(state.get("transaction_payload") or {})
    stable: Dict[str, Any] = dict(state.get("stable_entities") or {})
    working: Dict[str, Any] = dict(state.get("working_memory") or {})

    interpreted_event = str(state.get("interpreted_event") or "").upper().strip()
    expected_input = str(state.get("expected_input") or "NONE").upper().strip()

    # FAILLE 1 — Sanctuarise the active business goal.
    # Never overwrite an active tunnel goal with None/UNKNOWN because the user replied briefly ("1", "ok", "oui").
    incoming_goal = state.get("current_goal")
    previous_goal = working.get("active_goal") or working.get("locked_intent")
    in_tunnel = bool(previous_goal and expected_input not in {"", "NONE"})
    if (
        previous_goal
        and (incoming_goal in (None, "", "UNKNOWN") or interpreted_event in {"UNKNOWN", "ANSWER"})
        and (in_tunnel or str(state.get("status") or "").upper() in {"WAITING_INPUT", "WAITING_CONFIRMATION"})
    ):
        current_goal = previous_goal
    else:
        current_goal = incoming_goal

    # --- Merge extracted entities into payload ---
    for k, v in extracted.items():
        if v not in (None, "", [], {}):
            payload[k] = v
    payload = _normalize_quantity_to_kg(payload)

    phone = state.get("user_phone")
    if phone and not payload.get("phone"):
        payload["phone"] = str(phone)

    # --- ENTITY INHERITANCE : stable_entities → payload pour continuité ---
    # Pendant un tunnel, si le payload manque un champ stable, on l'injecte.
    # Cela évite de redemander le produit si l'utilisateur l'a déjà dit.
    if current_goal:
        for key in ("product", "unit_mentioned", "zone_name"):
            if not payload.get(key) and stable.get(key):
                payload[key] = stable[key]
                logger.debug("[MemoryUpdate] Inherited stable entity %s=%s", key, stable[key])

    # --- ZONE INJECTION from user profile ---
    # Si aucune zone n'est fournie et que le profil en a une, on l'utilise
    if not payload.get("zone_name") and not payload.get("zone"):
        profile_zone = state.get("zone_name")
        if profile_zone:
            payload["zone_name"] = str(profile_zone)

    # --- AG-UI: Selection index resolution via available_mapping ---
    sel_idx = extracted.get("selection_index") or payload.get("selection_index")
    sel_val = extracted.get("selected_value") or payload.get("selected_value")
    mapping = state.get("available_mapping") or {}
    mapping_kind = working.get("available_mapping_kind")

    if mapping and (sel_idx is not None or sel_val is not None):
        resolved_id = None
        if sel_idx is not None:
            resolved_id = mapping.get(str(sel_idx))
        if resolved_id is None and sel_val is not None:
            resolved_id = mapping.get(str(sel_val))

        if resolved_id:
            if mapping_kind == "auction":
                payload["auction_id"] = str(resolved_id)
            elif mapping_kind == "bid":
                payload["bid_id"] = str(resolved_id)
            elif mapping_kind == "stock":
                payload["stock_id"] = str(resolved_id)
            elif mapping_kind == "farm":
                payload["farm_id"] = str(resolved_id)
            elif mapping_kind == "intent_disambiguation":
                pass
            else:
                payload["resolved_id"] = str(resolved_id)
            payload.pop("selection_index", None)
            payload.pop("selected_value", None)

    # --- Update stable entities ---
    new_stable: Dict[str, Any] = {}
    for k in ("product", "unit_mentioned", "movement_type", "zone_name"):
        if extracted.get(k):
            new_stable[k] = extracted[k]

    # --- Conversation metrics ---
    working["last_event"] = state.get("interpreted_event")
    working["last_confidence"] = float(state.get("interpreter_confidence") or 0.0)
    entity_count = sum(1 for v in payload.values() if v not in (None, "", [], {}))
    working["payload_richness"] = entity_count
    if current_goal and current_goal != "DISAMBIGUATION_PENDING":
        working["active_goal"] = current_goal
        working["locked_intent"] = current_goal
        working.setdefault("step_index", 0)

    return {
        "transaction_payload": payload,
        "stable_entities": {**stable, **new_stable},
        "working_memory": working,
        "current_goal": current_goal,
    }


# =====================================================================
# NODE 6C — VALIDATOR
# =====================================================================

# Champs marqués `required` dans INTENT_CONFIG mais que le context_resolver
# sait auto-résoudre. Le validator les retire de la liste « manquants » : la
# question conversationnelle ne sera donc PAS posée à l'utilisateur si l'agent
# peut les déduire (ex: 1 seule ferme → farm_id auto-fillé en aval).
_AUTO_RESOLVABLE_FIELDS = frozenset({"farm_id", "phone"})


# Priorité d'interrogation : les champs les plus contraignants d'abord
_FIELD_PRIORITY: Dict[str, int] = {
    "product": 0, "quantity_mentioned": 1, "price_mentioned": 2,
    "unit_mentioned": 3, "zone_name": 4, "zone": 4,
    "farm_id": 5, "stock_id": 5, "auction_id": 5, "bid_id": 5,
    "cycle_id": 5, "movement_type": 6, "intervention_type": 6,
}


def _missing_fields_for_goal(goal: str, payload: Dict[str, Any]) -> List[str]:
    config = INTENT_CONFIG.get(goal) or {}
    required: List[str] = list(config.get("required") or [])
    missing = [
        f for f in required
        if f not in _AUTO_RESOLVABLE_FIELDS
        and payload.get(f) in (None, "", [], {})
    ]
    # Smart ordering : champs les plus importants en premier
    missing.sort(key=lambda f: _FIELD_PRIORITY.get(f, 99))
    return missing

async def validator(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Valide la complétude du payload transactionnel selon l'intention courante."""
    goal = state.get("current_goal")
    event = str(state.get("interpreted_event") or "").upper()
    payload: Dict[str, Any] = dict(state.get("transaction_payload") or {})

    # AG-UI Contract: If event is ANSWER, merge extracted_entities into payload
    if event == "ANSWER":
        entities = state.get("extracted_entities") or {}
        for k, v in entities.items():
            if v not in (None, "", [], {}):
                payload[k] = v

    if not goal:
        return {
            "status": "WAITING_INPUT",
            "response_strategy": "CLARIFICATION",
            "missing_fields": [],
            "validation_errors": [],
            "transaction_payload": payload,
            "ag_ui_component": None,
        }

    # -----------------------------------------------------------------
    # WRITE TUNNELS — FARM & STOCKS
    # -----------------------------------------------------------------
    # FARM_CREATE : tolère les alias conversationnels
    if goal == "FARM_CREATE":
        if payload.get("location") and not payload.get("zone"):
            payload["zone"] = payload.get("location")
        if payload.get("size") and not payload.get("surface"):
            payload["surface"] = payload.get("size")

    # Résolution proactive de farm_id (zéro identifiant côté user)
    required_for_goal = list((INTENT_CONFIG.get(goal) or {}).get("required") or [])
    if "farm_id" in required_for_goal and payload.get("farm_id") in (None, "", [], {}):
        farms_cache = state.get("user_farms_cache")
        farms: List[Dict[str, Any]] = farms_cache if isinstance(farms_cache, list) else []

        # Heuristic safe name match (only if user explicitly provided farm_name)
        farm_name_in = payload.get("farm_name")
        if farm_name_in and farms:
            def _n(s: Any) -> str:
                return re.sub(r"\s+", " ", str(s or "")).strip().casefold()

            target = _n(farm_name_in)
            matches = [f for f in farms if _n(f.get("name")) == target]
            if len(matches) == 1:
                payload["farm_id"] = str(matches[0].get("farm_id") or matches[0].get("id") or "")

        # If still missing, either auto-pick (single farm) or ask selection.
        if payload.get("farm_id") in (None, "", [], {}):
            if len(farms) == 1:
                only = farms[0]
                resolved = only.get("farm_id") or only.get("id")
                if resolved:
                    payload["farm_id"] = str(resolved)
            elif len(farms) > 1:
                # Ask user to choose farm via ListMenu.
                candidates: List[str] = []
                mapping: Dict[str, str] = {}
                for idx, farm in enumerate(farms, start=1):
                    name = str(farm.get("name") or "Exploitation").strip()
                    location = str(farm.get("location") or "").strip()
                    label = f"{name} — {location}" if location else name
                    candidates.append(label)
                    farm_id = farm.get("farm_id") or farm.get("id")
                    if farm_id:
                        mapping[str(idx)] = str(farm_id)

                wm = dict(state.get("working_memory") or {})
                wm["available_mapping_kind"] = "farm"
                return {
                    "status": "WAITING_INPUT",
                    "goal_status": "WAITING_INPUT",
                    "missing_fields": [],
                    "completed_fields": [f for f in required_for_goal if payload.get(f) not in (None, "", [], {})],
                    "validation_errors": [],
                    "last_missing_field": None,
                    "expected_input": "SELECTION",
                    "response_strategy": "SELECTION_MENU",
                    "expected_candidates": candidates,
                    "available_mapping": mapping,
                    "working_memory": wm,
                    "transaction_payload": payload,
                    "ag_ui_component": None,
                }

    # Short-circuits pour les intentions nécessitant une résolution d'ID proactive
    if goal == "MARKET_GET_REQUESTS" and not payload.get("auction_id"):
        return {
            "status": "PLANNING",
            "missing_fields": [],
            "last_missing_field": None,
            "expected_input": "NONE",
            "completed_fields": [k for k in ["product"] if payload.get(k)],
            "validation_errors": [],
            "transaction_payload": payload,
            "ag_ui_component": None,
        }

    if goal == "SALES_PLACE_BID" and payload.get("product") and not payload.get("auction_id"):
        return {
            "status": "PLANNING",
            "missing_fields": [],
            "last_missing_field": None,
            "expected_input": "NONE",
            "completed_fields": ["product"],
            "validation_errors": [],
            "transaction_payload": payload,
            "ag_ui_component": None,
        }

    if goal in {"MARKET_GET_MY_PROPOSALS", "SALES_ACCEPT_CONTRACT", "PROCUREMENT_ACCEPT_OFFER"} and not payload.get("bid_id"):
        return {
            "status": "PLANNING",
            "missing_fields": [],
            "last_missing_field": None,
            "expected_input": "NONE",
            "completed_fields": [],
            "validation_errors": [],
            "transaction_payload": payload,
            "ag_ui_component": None,
        }

    # Calcul dynamique des champs manquants depuis l'INTENT_CONFIG
    missing = _missing_fields_for_goal(goal, payload)
    required = list((INTENT_CONFIG.get(goal) or {}).get("required") or [])
    completed = [f for f in required if f not in missing]

    errors: List[str] = []
    warnings: List[str] = []
    price = payload.get("price_mentioned")
    if price is not None:
        try:
            pf = float(price)
            if pf <= 0:
                errors.append("Le prix doit être supérieur à 0.")
            elif pf > 10_000_000:
                warnings.append(f"Prix très élevé ({pf:,.0f} FCFA). Vérifiez.")
        except (TypeError, ValueError):
            errors.append("Le prix indiqué n'est pas un nombre valide.")

    qty = payload.get("quantity_mentioned")
    if qty is not None:
        try:
            qf = float(qty)
            if qf <= 0:
                errors.append("La quantité doit être supérieure à 0.")
            elif qf > 100_000:
                warnings.append(f"Quantité très importante ({qf:,.0f}). Vérifiez l'unité.")
        except (TypeError, ValueError):
            errors.append("La quantité indiquée n'est pas un nombre valide.")
    payload = _normalize_quantity_to_kg(payload)

    # Cross-field : unité cohérente avec quantité
    unit_raw = str(payload.get("unit_mentioned") or "KG").upper()
    if qty is not None and unit_raw == "TONNE":
        try:
            if float(qty) > 500:
                warnings.append("500+ tonnes semble excessif. Vérifiez l'unité.")
        except (TypeError, ValueError):
            pass

    # Progress computation
    progress = _compute_progress(goal, payload)

    if missing or errors:
        first_missing = missing[0] if missing else None
        expected_input_map = {
            "product": "PRODUCT",
            "price_mentioned": "PRICE",
            "quantity_mentioned": "QUANTITY",
            "unit_mentioned": "UNIT",
            "zone_name": "LOCATION",
        }
        hint = None
        if progress and first_missing:
            total = progress.get("total", 0)
            filled = progress.get("filled", 0)
            label = _label_for_field(goal, first_missing)
            hint = f"Étape {filled + 1}/{total} : {label}"

        return {
            "status": "WAITING_INPUT",
            "goal_status": "WAITING_INPUT",
            "missing_fields": missing,
            "completed_fields": completed,
            "validation_errors": errors + warnings,
            "last_missing_field": first_missing,
            "expected_input": expected_input_map.get(first_missing or "", "NONE"),
            "response_strategy": "ASK_MISSING_FIELD",
            "transaction_payload": payload,
            "conversation_progress": progress,
            "proactive_hint": hint,
            "ag_ui_component": None,
        }

    # All valid — inject warnings as advisory
    validation_warns = warnings if warnings else []

    return {
        "status": "PLANNING",
        "missing_fields": [],
        "last_missing_field": None,
        "expected_input": "NONE",
        "completed_fields": completed,
        "validation_errors": validation_warns,
        "transaction_payload": payload,
        "conversation_progress": progress,
        "proactive_hint": "Toutes les infos sont là. Confirmation en cours..." if not validation_warns else validation_warns[0],
        "ag_ui_component": None,
    }


# =====================================================================
# NODE 8 — CONFIRMATION GATE (AG-UI NATIVE)
# =====================================================================

def _build_confirmation_summary(goal: str, payload: Dict[str, Any]) -> str:
    product = payload.get("product")
    qty = payload.get("quantity_mentioned")
    unit = payload.get("unit_mentioned") or "KG"
    price = payload.get("price_mentioned")

    mapping = {
        "SALES_PUBLISH_PRODUCT": f"Vente de {qty} {unit} de {product} à {price} FCFA/{unit}.",
        "SALES_RECORD_DIRECT": f"Enregistrement d'une vente directe : {qty} {unit} de {product} à {price} FCFA.",
        "PROCUREMENT_CREATE_REQUEST": f"Lancement d'un appel d'offres pour {qty} {unit} de {product} au prix plafond de {price} FCFA.",
        "SALES_PLACE_BID": f"Soumission d'une offre de {price} FCFA sur cette enchère.",
        "SALES_ACCEPT_CONTRACT": "Validation finale du contrat avec l'acheteur.",
        "PROCUREMENT_ACCEPT_OFFER": "Acceptation de l'offre du producteur sélectionné.",
        "PROCUREMENT_SELECT_WINNER": "Sélection de l'offre gagnante.",
        "STOCK_REGISTER_HARVEST": f"Enregistrement d'une récolte : {qty} {unit} de {product} en stock.",
        "STOCK_RECORD_MOVEMENT": f"Mouvement de stock : {qty} {unit} de {product}.",
        "STOCK_ADJUST": f"Modification du stock de {product} à {qty} {unit}.",
        "STOCK_REMOVE_PARTIAL": f"Retrait de {qty} {unit} de {product} du stock.",
        "STOCK_DELETE": f"Suppression définitive du lot de {product}.",
        "FINANCE_LOG_EXPENSE": f"Enregistrement d'une dépense de {price} FCFA ({product}).",
        "FARM_CREATE": f"Déclaration d'une nouvelle exploitation.",
        "CROP_RECORD_INTERVENTION": f"Enregistrement d'une intervention agronomique.",
    }
    return mapping.get(goal, f"Validation de l'opération : {goal}")

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
                "transaction_payload": {},
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


# =====================================================================
# NODE 9 — MCP TOOL EXECUTOR HELPERS
# =====================================================================

def _extract_tool_schema(tool_item: Any) -> Dict[str, Any]:
    """Extrait proprement le schéma de validation d'un outil MCP."""
    if isinstance(tool_item, dict):
        for key in ("inputSchema", "input_schema", "parameters"):
            if isinstance(tool_item.get(key), dict):
                return tool_item[key]
        fn = tool_item.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("parameters"), dict):
            return fn["parameters"]
    for attr in ("inputSchema", "input_schema", "parameters"):
        schema = getattr(tool_item, attr, None)
        if isinstance(schema, dict):
            return schema
    return {"type": "object", "properties": {}, "required": []}


def _extract_tool_name(tool_item: Any) -> Optional[str]:
    """Extrait le nom identifiant de l'outil MCP."""
    if isinstance(tool_item, dict):
        fn = tool_item.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            return str(fn["name"])
        if tool_item.get("name"):
            return str(tool_item["name"])
    name = getattr(tool_item, "name", None)
    return str(name) if name else None


async def _list_mcp_tools(mc_runtime: Any) -> List[Any]:
    """Liste l'ensemble des outils déclarés sur le client MCP."""
    client = getattr(mc_runtime, "db_client", None)
    if client is None:
        return []

    # Prefer list_tools() (returns OpenAI-format dicts with schema)
    for method_name in ("list_tools", "get_tools_for_langchain"):
        fn = getattr(client, method_name, None)
        if fn is None:
            continue
        raw = fn()
        if inspect.isawaitable(raw) or asyncio.iscoroutine(raw):
            raw = await raw
        if isinstance(raw, list) and raw:
            return raw

    # Fallback: try a dict-keyed response
    raw = getattr(client, "_tools_cache", None)
    if isinstance(raw, list) and raw:
        return raw
    return []


async def _get_mcp_tool_schema(mc_runtime: Any, tool_name: str) -> Dict[str, Any]:
    """Récupère le schéma associé à un outil spécifique ou renvoie un fallback vide."""
    tools = await _list_mcp_tools(mc_runtime)
    for tool_item in tools:
        if _extract_tool_name(tool_item) == tool_name:
            return _extract_tool_schema(tool_item)
    return {"type": "object", "properties": {}, "required": []}


def _lookup_arg_value(param_name: str, state: Dict[str, Any], payload: Dict[str, Any], initial_args: Dict[str, Any]) -> Any:
    """Recherche une valeur de paramètre de manière stricte (AG-UI).

    Résolution d'identité hiérarchique :
    - producer_id, user_id → préfère UUID du profil (state.user_id)
    - phone, user_phone → state.user_phone
    - Fallback : cherche dans initial_args, payload, entities, state
    """
    # UUID-based identity: prefer real UUID over phone
    if param_name in {"producer_id", "user_id"}:
        # First try real UUID from profile
        uuid_val = state.get("user_id")
        if uuid_val:
            return uuid_val
        # Fallback to phone (schema resolver may still work)
        if state.get("user_phone"):
            return state.get("user_phone")

    if param_name in {"user_phone", "phone"} and state.get("user_phone"):
        return state.get("user_phone")

    sources = [initial_args, payload, state.get("extracted_entities") or {}, state]
    candidate_names = [param_name] + _ARG_ALIASES.get(param_name, [])
    for candidate_name in candidate_names:
        for source in sources:
            if source and source.get(candidate_name) not in (None, "", [], {}):
                return source[candidate_name]
    return None


def _cast_arg_value(value: Any, json_type: str) -> Any:
    """Type et nettoie les arguments pour correspondre rigoureusement aux attentes SQL."""
    if value in (None, "", [], {}):
        return None
    try:
        if json_type == "number":
            return float(str(value).replace(",", ".").replace(" ", ""))
        if json_type == "integer":
            return int(float(str(value).replace(",", ".").replace(" ", "")))
        if json_type == "boolean":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "oui", "y", "on"}
        return str(value)
    except Exception:
        return value


_POSTEL_DEFAULTS: Dict[str, Any] = {
    "string": "",
    "number": 0.0,
    "integer": 0,
    "boolean": False,
    "array": [],
    "object": {},
}


_IDENTITY_ALIASES = frozenset({"user_phone", "phone", "user_id", "producer_id"})
_ARG_ALIASES: Dict[str, List[str]] = {
    "phone": ["user_phone"],
    "user_id": ["producer_id"],
    "producer_id": ["user_id"],
    "product_name": ["product", "name", "item_name", "product_query"],
    "product": ["product_name", "name", "item_name", "product_query"],
    "item_name": ["product", "product_name", "name"],
    # FAILLE 3B — Restrict aliases to avoid semantic collisions.
    # `farm_name` must NEVER be treated as a product `name`.
    "name": ["product", "product_name", "item_name"],
    "farm_name": ["farm_label", "farm_title"],
    "quantity": ["quantity_mentioned", "qty", "quantity_for_sale"],
    "qty": ["quantity_mentioned", "quantity"],
    "quantity_for_sale": ["quantity_mentioned", "quantity", "qty"],
    "unit": ["unit_mentioned"],
    "price": ["price_mentioned", "max_price", "offered_price", "proposed_price"],
    "max_price": ["price_mentioned", "price", "target_price"],
    "offered_price": ["price_mentioned", "price"],
    "proposed_price": ["price_mentioned", "price"],
    "zone": ["zone_name", "zone_query", "zone_id"],
    "zone_name": ["zone", "zone_query", "zone_id"],
    "zone_query": ["zone_name", "zone"],
    "zone_id": ["zone_name", "zone"],
    "latitude": ["lat"],
    "longitude": ["lon", "lng"],
    "lat": ["latitude"],
    "lon": ["longitude", "lng"],
    "transaction_id": ["bid_id", "staging_id"],
    "staging_id": ["transaction_id", "bid_id"],
}


class MissingRequiredMCPArgs(ValueError):
    def __init__(self, tool_name: str, missing_args: List[str]):
        self.tool_name = tool_name
        self.missing_args = missing_args
        super().__init__(f"Missing required MCP args for {tool_name}: {', '.join(missing_args)}")


def _build_resolved_tool_args(tool_name: str, schema: Dict[str, Any], state: Dict[str, Any], payload: Dict[str, Any], initial_args: Dict[str, Any]) -> Dict[str, Any]:
    """Construit le dictionnaire final d'arguments validés par rapport au schéma.

        Apply a *strict* contract:
        - Optional fields that are None are omitted entirely.
        - Required fields MUST be present with a meaningful value (or a schema default).
            Never inject phantom defaults like "", 0, 0.0 to satisfy the schema.
        - All values are cast to schema-declared types.
    """
    properties: Dict[str, Any] = schema.get("properties") or {}
    required: List[str] = list(schema.get("required") or [])

    if not properties:
        # Fallback: no schema available — sanitize initial_args (remove None)
        return {k: v for k, v in (initial_args or {}).items() if v is not None}

    resolved_args: Dict[str, Any] = {}
    resolved_identity = False
    missing_required: List[str] = []
    for param_name, param_schema in properties.items():
        json_type = str(param_schema.get("type") or "string").lower()
        value = _lookup_arg_value(param_name, state, payload, initial_args)

        if param_name in _IDENTITY_ALIASES and value is not None:
            resolved_identity = True

        if value in (None, "", [], {}):
            # FAILLE 3A — Do not inject empty/zero values.
            # If required, only allow a *meaningful* schema default; otherwise mark missing.
            if param_name in required:
                default = param_schema.get("default")
                if default not in (None, "", [], {}):
                    resolved_args[param_name] = _cast_arg_value(default, json_type)
                else:
                    missing_required.append(param_name)
            continue

        resolved_args[param_name] = _cast_arg_value(value, json_type)

    if missing_required:
        raise MissingRequiredMCPArgs(tool_name, missing_required)

    return resolved_args


def _sanitize_mcp_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Final Postel's Law gate before MCP call: remove None, ensure str for str fields."""
    return {k: v for k, v in args.items() if v is not None}


_ASCII_FOLD_TRANSLATION = str.maketrans({
    "œ": "oe",
    "Œ": "OE",
    "æ": "ae",
    "Æ": "AE",
    "’": "'",
    "“": '"',
    "”": '"',
})


def _ascii_fold_str(text: str) -> str:
    """Strip accents/ligatures to keep MCP arguments ASCII-only."""
    if not text:
        return text
    text = text.translate(_ASCII_FOLD_TRANSLATION)
    normalized = unicodedata.normalize("NFKD", text)
    return normalized.encode("ascii", "ignore").decode("ascii")


def _ascii_fold_value(value: Any) -> Any:
    if isinstance(value, str):
        return _ascii_fold_str(value)
    if isinstance(value, list):
        return [_ascii_fold_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_ascii_fold_value(v) for v in value)
    if isinstance(value, dict):
        return {k: _ascii_fold_value(v) for k, v in value.items()}
    return value


# =====================================================================
# MCP ERROR TRANSLATION + POST-SUCCESS SUGGESTIONS
# =====================================================================

_MCP_ERROR_TRANSLATIONS: Dict[str, str] = {
    "not_found": "L'élément demandé n'a pas été trouvé. Vérifiez les données ou reformulez.",
    "duplicate": "Cet enregistrement existe déjà. Voulez-vous le modifier plutôt ?",
    "permission": "Vous n'avez pas les droits pour cette opération.",
    "invalid": "Les données envoyées ne sont pas valides. Vérifiez et réessayez.",
    "stock": "Problème lié au stock. Vérifiez vos quantités.",
    "closed": "Cette enchère ou offre est déjà clôturée.",
}


def _translate_mcp_error(raw_error: str) -> str:
    """Traduit une erreur MCP brute en message utilisateur compréhensible."""
    lower = (raw_error or "").lower()
    for key, msg in _MCP_ERROR_TRANSLATIONS.items():
        if key in lower:
            return msg
    return _GENERIC_TECHNICAL_ERROR


_POST_SUCCESS_SUGGESTIONS: Dict[str, str] = {
    "SALES_PUBLISH_PRODUCT": "Astuce : consultez vos offres en disant \"voir mon stock\".",
    "PROCUREMENT_CREATE_REQUEST": "Vous serez notifié dès qu'un producteur répond.",
    "SALES_PLACE_BID": "Vous pouvez suivre vos offres avec \"mes enchères\".",
    "STOCK_REGISTER_HARVEST": "Vous pouvez maintenant mettre en vente avec \"publier produit\".",
    "STOCK_RECORD_MOVEMENT": "Votre inventaire a été mis à jour.",
}


def _post_success_suggestion(goal: str, payload: Dict[str, Any]) -> Optional[str]:
    """Génère une suggestion proactive après le succès d'une opération."""
    return _POST_SUCCESS_SUGGESTIONS.get(goal)


# =====================================================================
# SELF-HEALING ARG REPAIR — Tentative de réparation autonome des args
# =====================================================================

def _attempt_arg_repair(raw_value: Any, expected_type: str) -> Optional[Any]:
    """Tente de réparer un argument mal typé SANS appel LLM.

    Exemples:
      - "100kg" → 100.0 (pour expected_type='number')
      - "2 tonnes" → 2000.0
      - "trois" → 3.0 (mots-nombres fr courants)
    """
    if raw_value is None:
        return None
    s = str(raw_value).strip().lower()

    if expected_type in ("number", "integer"):
        # Strip common unit suffixes
        for suffix in ("kg", "tonnes", "tonne", "t", "sacs", "sac", "fcfa", "cfa", "f"):
            if s.endswith(suffix):
                s = s[:-len(suffix)].strip()
                break
        # Handle comma as decimal separator
        s = s.replace(",", ".").replace(" ", "").replace("\xa0", "")
        # French word numbers
        _WORD_NUMS = {"un": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
                      "six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10,
                      "vingt": 20, "trente": 30, "cinquante": 50, "cent": 100, "mille": 1000}
        if s in _WORD_NUMS:
            return float(_WORD_NUMS[s]) if expected_type == "number" else _WORD_NUMS[s]
        try:
            val = float(s)
            return val if expected_type == "number" else int(val)
        except (ValueError, TypeError):
            return None

    if expected_type == "boolean":
        return s in ("1", "true", "yes", "oui", "ok", "y")

    return str(raw_value)  # string: return as-is


# =====================================================================
# NODE 9 — MCP TOOL EXECUTOR (Self-Healing + Retry)
# =====================================================================

async def mcp_tool_executor(state: Dict[str, Any], mc_runtime: Any) -> Dict[str, Any]:
    """Exécuteur MCP agentique avec auto-réparation d'arguments.

    Capacités :
    1. Dispatcher → args préparés
    2. Schema resolution → args validés
    3. Self-healing sur dispatcher ValueError (tente résolution alternative)
    4. Auto-retry sur erreurs transitoires (timeout, 502, 503)
    5. Arg repair sur rejet MCP (parse erreur, corrige, réessaie)
    6. Traduction erreur MCP → français user-friendly
    7. Suggestion proactive post-succès
    """
    if not state.get("execution_authorized"):
        logger.warning("Executor invoqué sans autorisation — refus d'écriture")
        return {
            "status": "ERROR",
            "validation_errors": ["execution_not_authorized"],
            "response_strategy": "ERROR",
            "ag_ui_component": None,
        }

    goal = (state.get("current_goal") or "").upper()
    payload: Dict[str, Any] = state.get("transaction_payload") or {}
    phone = state.get("user_phone")
    retry_count = int(state.get("retry_count") or 0)

    prep = MARKET_WRITE_ACTIONS_MAP.get(goal) or MARKET_READ_ACTIONS_MAP.get(goal)
    if prep is None:
        logger.error("Aucun dispatcher trouvé pour goal=%s", goal)
        return {
            "status": "ERROR",
            "validation_errors": [f"no_dispatcher_for_{goal}"],
            "response_strategy": "ERROR",
            "selected_tool": None,
            "selected_tool_args": {},
            "ag_ui_component": None,
        }

    # --- SELF-HEALING DISPATCHER CALL ---
    # If dispatcher raises ValueError (missing field), attempt repair from state
    try:
        tool_name, tool_args = prep(payload, str(phone or ""))
    except ValueError as ve:
        # Analyze which field is missing and attempt to find it in state/profile
        missing_field = str(ve).replace("Missing required field: ", "").strip()
        logger.info("[SelfHeal] Dispatcher ValueError for %s: missing '%s' — attempting repair", goal, missing_field)

        # Try to resolve from stable_entities, working_memory, or profile
        repair_sources = [
            state.get("stable_entities") or {},
            state.get("working_memory") or {},
            state.get("extracted_entities") or {},
        ]
        repaired_value = None
        for source in repair_sources:
            candidate = source.get(missing_field)
            if candidate not in (None, "", [], {}):
                repaired_value = candidate
                break

        if repaired_value is not None:
            # Inject repaired value and retry dispatcher
            payload[missing_field] = repaired_value
            logger.info("[SelfHeal] Repaired '%s' = %r from state — retrying dispatcher", missing_field, repaired_value)
            try:
                tool_name, tool_args = prep(payload, str(phone or ""))
            except Exception as exc2:
                logger.error("[SelfHeal] Dispatcher still fails after repair: %s", exc2)
                return {
                    "status": "ERROR",
                    "validation_errors": [f"dispatcher_error_after_repair: {exc2}"],
                    "response_strategy": "ERROR",
                    "final_response": _GENERIC_TECHNICAL_ERROR,
                    "selected_tool": None,
                    "selected_tool_args": {},
                    "ag_ui_component": None,
                }
        else:
            # Cannot self-heal — route back to slot-filling
            logger.warning("[SelfHeal] Cannot repair '%s' — routing to ASK_MISSING_FIELD", missing_field)
            return {
                "status": "WAITING_INPUT",
                "missing_fields": [missing_field],
                "last_missing_field": missing_field,
                "response_strategy": "ASK_MISSING_FIELD",
                "validation_errors": [],
                "selected_tool": None,
                "selected_tool_args": {},
                "ag_ui_component": None,
            }
    except Exception as exc:
        logger.error("Le dispatcher pour %s a échoué: %s", goal, exc)
        return {
            "status": "ERROR",
            "validation_errors": [f"dispatcher_error: {exc}"],
            "response_strategy": "ERROR",
            "final_response": _GENERIC_TECHNICAL_ERROR,
            "selected_tool": None,
            "selected_tool_args": {},
            "ag_ui_component": None,
        }

    tool_schema = await _get_mcp_tool_schema(mc_runtime, tool_name)
    try:
        resolved_args = _build_resolved_tool_args(
            tool_name=tool_name,
            schema=tool_schema,
            state=state,
            payload=payload,
            initial_args=tool_args or {},
        )
    except MissingRequiredMCPArgs as exc:
        # Controlled failure: route back to slot-filling instead of sending nonsense.
        logger.warning("[Executor] %s", str(exc))

        # Map MCP param names to conversational slots when possible.
        mcp_to_slot = {
            "name": "product",
            "product_name": "product",
            "quantity_for_sale": "quantity_mentioned",
            "quantity": "quantity_mentioned",
            "price": "price_mentioned",
            "producer_id": "phone",
            "user_id": "phone",
        }
        missing_slots = [mcp_to_slot.get(m, m) for m in (exc.missing_args or [])]
        missing_slots = [m for m in missing_slots if m]

        first_missing = missing_slots[0] if missing_slots else None
        return {
            "status": "WAITING_INPUT",
            "execution_authorized": False,
            "validation_errors": [f"missing_required_args: {', '.join(exc.missing_args)}"],
            "selected_tool": tool_name,
            "selected_tool_args": {},
            "missing_fields": missing_slots,
            "last_missing_field": first_missing,
            "response_strategy": "ASK_MISSING_FIELD",
            "ag_ui_component": None,
        }
    # Final Postel's Law gate: never pass None to MCP
    resolved_args = _sanitize_mcp_args(resolved_args)
    # AXE 4: ensure MCP never receives accented strings
    resolved_args = _ascii_fold_value(resolved_args)

    logger.info(
        "MCP_EXEC_AUDIT | tool=%s | args=%s",
        tool_name,
        json.dumps(resolved_args, default=str, ensure_ascii=False),
    )
    history = list(state.get("tool_execution_history") or [])

    # --- AUTO-RETRY loop for transient failures ---
    _MCP_MAX_TRANSIENT_RETRIES = 2
    _TRANSIENT_MARKERS = ("timeout", "connection", "unavailable", "temporary", "503", "502")
    last_exc: Optional[Exception] = None

    for attempt in range(1, _MCP_MAX_TRANSIENT_RETRIES + 1):
        try:
            raw = await mc_runtime.call_db(tool_name, **resolved_args)
            result = ensure_dict(raw)
            success = is_success_response(result)

            history.append({
                "tool": tool_name,
                "args": resolved_args,
                "success": success,
                "raw": result,
                "ts": _now(),
                "attempt": attempt,
            })

            if not success:
                err_msg = result.get("message") or result.get("error") or "Transaction rejetée par le système"
                logger.warning("Tool %s rejeté par le MCP (tentative %d): %s", tool_name, attempt, err_msg)
                # Translate MCP errors to user-friendly advice
                user_msg = _translate_mcp_error(str(err_msg))
                return {
                    "status": "ERROR",
                    "execution_result": result,
                    "selected_tool": tool_name,
                    "selected_tool_args": resolved_args,
                    "tool_execution_history": history,
                    "retry_count": retry_count,
                    "validation_errors": [str(err_msg)],
                    "response_strategy": "ERROR",
                    "final_response": user_msg,
                    "ag_ui_component": None,
                }

            # --- POST-SUCCESS proactive suggestion ---
            proactive = _post_success_suggestion(goal, payload)

            success_state: Dict[str, Any] = {
                "status": "COMPLETED",
                "goal_status": "COMPLETED",
                "execution_result": result,
                "selected_tool": tool_name,
                "selected_tool_args": resolved_args,
                "tool_execution_history": history,
                "retry_count": retry_count,
                "is_certified": False,
                "execution_authorized": False,
                "waiting_for_confirmation": False,
                "transaction_payload": {},
                "missing_fields": [],
                "completed_fields": [],
                "expected_input": "NONE",
                "current_goal": None,
                "response_strategy": "SUCCESS",
                "proactive_hint": proactive,
                "ag_ui_component": None,
            }

            if isinstance(result.get("mapping"), dict):
                success_state["available_mapping"] = result["mapping"]
                kind = "auction" if "auction" in tool_name else "bid"
                success_state["working_memory"] = {"available_mapping_kind": kind}

            return success_state

        except Exception as exc:
            last_exc = exc
            exc_lower = str(exc).lower()
            is_transient = any(m in exc_lower for m in _TRANSIENT_MARKERS)
            history.append({
                "tool": tool_name,
                "args": resolved_args,
                "success": False,
                "error": str(exc),
                "ts": _now(),
                "attempt": attempt,
                "transient": is_transient,
            })
            if is_transient and attempt < _MCP_MAX_TRANSIENT_RETRIES:
                logger.warning(
                    "[Executor] Transient error on %s (attempt %d/%d): %s — retrying",
                    tool_name, attempt, _MCP_MAX_TRANSIENT_RETRIES, exc,
                )
                await asyncio.sleep(0.5 * attempt)
                continue
            break

    # All retries exhausted
    logger.exception("Le call MCP %s a crashé après %d tentatives: %s", tool_name, _MCP_MAX_TRANSIENT_RETRIES, last_exc)
    return {
        "status": "ERROR",
        "selected_tool": tool_name,
        "selected_tool_args": resolved_args,
        "tool_execution_history": history,
        "retry_count": retry_count + 1,
        "validation_errors": [f"mcp_crash: {last_exc}"],
        "execution_authorized": False,
        "response_strategy": "ERROR",
        "final_response": _translate_mcp_error(str(last_exc)),
        "ag_ui_component": None,
    }


# =====================================================================
# NODE 10 — RESPONSE STRATEGY (Routing AG-UI pur)
# =====================================================================

# (moved) `response_strategy` is implemented in `intent_router.py`.


# =====================================================================
# LLM QUESTION GENERATOR (AG-UI Agentic Semantics)
# =====================================================================


# (moved) Pedagogical question generation + field reasons live in `response_handlers.py`.


# =====================================================================
# NODE 11 — FINAL RESPONSE (AG-UI Component Injection + LLM Semantics)
# =====================================================================

# (moved) `final_response` is implemented in `response_handlers.py`.


# =====================================================================
# ROUTING HELPERS (utilisés par graph_builder.py)
# =====================================================================

def _route_after_security(state: MarketAgentState) -> str:
    """Achemine vers le routeur de stratégie si une fraude est détectée."""
    if state.get("security_status") == "SCAM_DETECTED":
        return "to_strategy"
    return "to_interpreter"


def _route_after_planner(state: MarketAgentState) -> str:
    """Achemine vers le routeur si des informations critiques manquent."""
    status = str(state.get("status") or "").upper()
    if status == "WAITING_INPUT":
        return "to_strategy"
    return "to_memory"


def _route_after_resolver(state: MarketAgentState) -> str:
    """Redirige si l'état nécessite une interaction ou s'il est prêt pour confirmation."""
    status = str(state.get("status") or "").upper()
    if status in {"WAITING_INPUT", "ERROR", "COMPLETED"}:
        return "to_strategy"
    return "to_confirmation"


def _route_after_confirmation(state: MarketAgentState) -> str:
    """Achemine vers l'exécuteur MCP si l'opération a été validée par l'utilisateur."""
    status = str(state.get("status") or "").upper()
    if status == "EXECUTING":
        return "to_executor"
    return "to_strategy"


def _route_after_executor(state: MarketAgentState) -> str:
    """Routage post-exécution agentique.

    Si l'executor a détecté un champ manquant via self-healing (status=WAITING_INPUT),
    il route vers response_strategy pour poser la question.
    Si erreur récupérable avec retry possible, re-route vers validator pour correction.
    Sinon : stratégie normale.
    """
    status = str(state.get("status") or "").upper()
    # Self-healing a routé vers ASK_MISSING_FIELD
    if status == "WAITING_INPUT":
        return "to_strategy"
    # TODO: future feedback loop — route to validator on recoverable errors
    return "to_strategy"




__all__ = [
    # Constants
    "_WRITE_GOALS",
    "_READ_GOALS",
    # Helpers
    "_now",
    "_normalize_text",
    "_safe_node",
    "_maybe_await",
    "reset_error_status",
    # Nodes
    "input_normalizer",
    "security_moderation",
    "cognitive_guard",
    "semantic_disambiguation",
    "clarification_node",
    "onboarding_node",
    "memory_update",
    "validator",
    "confirmation_gate",
    "mcp_tool_executor",
    "response_strategy",
    "final_response",
    # Routing
    "_route_after_security",
    "_route_after_planner",
    "_route_after_resolver",
    "_route_after_confirmation",
    "_route_after_executor",
]
