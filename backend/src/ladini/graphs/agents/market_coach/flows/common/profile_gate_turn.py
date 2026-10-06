"""Un tour de collecte JUSTE-À-TEMPS du profil (« mode gate » de `onboarding_node`).

Le gate est une BARRIÈRE PAUSE/REPRISE, jamais un RESET ni une reconstruction :

    BUSINESS_ACTIVE ─(profil manquant)─► PAUSED_FOR_PROFILE ─(champ fourni)─► il manque encore ? ─oui─► question suivante
                                                                                      │ non
                                                                                      ▼
                                                                         BUSINESS_RESUMED (même commande)

La commande en pause (`profile_gate["command"]`, capturée par `core/profile_gate.py`) est restaurée À L'IDENTIQUE à la reprise :
objectif, intention, événement, payload, entités. Le message de profil (« Mon nom c'est Zouba ») est un FAIT de profil — il ne
sert JAMAIS à reconstruire la commande. Si le contexte de reprise est invalide, on échoue FERMÉ (message sûr, panier conservé).

Aucun nouveau moteur : lecture du slot (`profile_slot.py`), résolveur des 17 régions (`agents/onboarding.py::_resolve_zone`),
outil MCP `complete_user_profile`, reprise par `context_resolver`.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, Optional

from ladini.agents.onboarding import _resolve_zone
from ladini.domain.profile_requirements import (
    ProfileAction,
    ProfileFacts,
    ProfileField,
    get_missing_requirements,
)
from ladini.graphs.agents.market_coach.core.profile_gate import (
    PLACEHOLDER_ZONE_VALUES,
    command_is_resumable,
    question_for,
    why_we_ask,
)
from ladini.graphs.agents.market_coach.core.state import lock_goal
from ladini.graphs.agents.market_coach.flows.common.profile_slot import (
    ANSWER,
    INTERRUPTION,
    QUESTION,
    REFUSAL,
    read_profile_slot,
)
from ladini.graphs.agents.market_coach.services.mcp.gateway import ProfileGateway

logger = logging.getLogger("Ladini.Market.ProfileGateTurn")

_RESTORED_KEYS = ("detected_intent", "interpreted_event", "interpreter_confidence", "transaction_payload", "extracted_entities")
_PLACE_KEYS = ("zone", "region", "localite", "location", "zone_name", "target_zone")
#: Au-delà, un message qui n'est jamais une réponse libère l'utilisateur (jamais prisonnier du mini-parcours).
_MAX_UNCLEAR = 2

SAFE_RESUME_FAILURE = (
    "Je n'ai pas pu reprendre correctement ta demande. Ton panier est toujours conservé — redis-moi simplement "
    "ce que tu veux faire pour continuer."
)


def _ask(gate: Dict[str, Any], text: str) -> Dict[str, Any]:
    return {
        "profile_gate": gate,
        "is_onboarding": True,
        "status": "WAITING_INPUT",
        "response_strategy": "ONBOARDING",
        # `render_onboarding` (stratégie ONBOARDING) lit `onboarding_prompt`.
        "onboarding_prompt": text,
        "final_response": text,
        "ag_ui_component": None,
    }


def _release(reason: str, gate: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("profile_gate_released | goal=%s | reason=%s", gate.get("goal"), reason)
    return {"profile_gate": None, "is_onboarding": False, "profile_gate_outcome": "RELEASE"}


def _restore_place(payload: Dict[str, Any], region: Optional[str]) -> Dict[str, Any]:
    """Le payload porte la région du profil (jamais le repli « Zone inconnue ») sans toucher au reste du payload métier."""
    if not region:
        return payload
    out = dict(payload)
    for key in _PLACE_KEYS:
        if key in out and str(out.get(key) or "").strip().lower() in PLACEHOLDER_ZONE_VALUES:
            out[key] = region
    return out


def build_resume_patch(state: Dict[str, Any], gate: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    """Restaure la commande capturée À L'IDENTIQUE (hors nom/région/capacités, enrichis volontairement)."""
    command = dict(gate.get("command") or {})
    goal = command.get("current_goal") or command.get("goal") or gate.get("goal")
    region = updates.get("declared_location") or state.get("declared_location")
    patch: Dict[str, Any] = {
        **updates,
        "profile_gate": None,
        "is_onboarding": False,
        "profile_gate_outcome": "RESUME",
        "current_goal": goal,
        "status": command.get("status") or "PLANNING",
        "working_memory": lock_goal(dict(state.get("working_memory") or {}), goal),
    }
    for key in _RESTORED_KEYS:
        if key in command:
            patch[key] = copy.deepcopy(command[key])
    if isinstance(patch.get("transaction_payload"), dict):
        patch["transaction_payload"] = _restore_place(patch["transaction_payload"], region)
    return patch


async def profile_gate_turn(state: Dict[str, Any], mc_runtime: Any) -> Dict[str, Any]:
    gate: Dict[str, Any] = dict(state.get("profile_gate") or {})
    action = ProfileAction(gate.get("action") or ProfileAction.DISCOVERY.value)
    missing = [ProfileField(m) for m in gate.get("missing") or []]
    if not missing:  # état incohérent : ne bloque jamais l'utilisateur
        return _release("empty_requirements", gate)
    asking = missing[0]
    question = question_for(action, asking, intro=False)
    text = str(state.get("normalized_text") or state.get("user_query") or "").strip()
    phone = str(state.get("user_phone") or "").strip()

    reading = await read_profile_slot(mc_runtime, text, asking, question=question)

    if reading.kind == INTERRUPTION:
        return _release("interruption", gate)
    if reading.kind == REFUSAL:
        return _ask(gate, f"Pas de souci. J'ai besoin de cette information pour continuer, ta demande reste en attente.\n\n{question}")
    if reading.kind == QUESTION:
        return _ask(gate, f"{why_we_ask(action, asking)}\n\n{question}")
    if reading.kind != ANSWER or not reading.value:
        if reading.source == "fallback":  # lecture SANS modèle : dans le doute on libère l'utilisateur, jamais on le retient
            return _release("unreadable_without_llm", gate)
        gate["unclear"] = int(gate.get("unclear") or 0) + 1
        if gate["unclear"] > _MAX_UNCLEAR:
            return _release("not_an_answer", gate)
        return _ask(gate, f"Je n'ai pas bien compris. {question}")

    fields: Dict[str, Any] = {}
    updates: Dict[str, Any] = {}
    if asking is ProfileField.NAME:
        fields["name"] = reading.value
        updates["user_name"] = reading.value
    else:
        resolution = await _resolve_zone(reading.value, mc_runtime, allow_clarify=not gate.get("region_clarified"))
        if resolution.status == "NEEDS_REGION":
            gate["region_clarified"] = True
            return _ask(gate, resolution.question or question)
        declared = resolution.declared or reading.value
        fields.update(zone_id=resolution.zone_id, declared_location=declared, coverage=resolution.status)
        updates.update(zone_id=resolution.zone_id, zone_name=resolution.zone_name or declared, declared_location=declared)

    logger.info("profile_gate_requirement | field=%s | goal=%s | action=%s | satisfied=true", asking.value, gate.get("goal"), action.value)
    still = get_missing_requirements(action, ProfileFacts.from_state({**state, **updates}))
    if phone:
        try:
            capability = gate.get("capability") if not still else None
            await ProfileGateway(mc_runtime).complete_user_profile(phone, **fields, **({"capability": capability} if capability else {}))
        except Exception as exc:  # noqa: BLE001 - le profil sera redemandé : jamais silencieusement perdu
            logger.warning("complete_user_profile a échoué : %s", exc)
            return _ask(gate, "Un incident technique m'empêche d'enregistrer cette information. Peux-tu me la redire dans un instant ?")

    if still:
        gate["missing"] = [f.value for f in still]
        gate["unclear"] = 0
        ack = f"Merci {updates['user_name']} ✅\n\n" if "user_name" in updates else ""
        return {**updates, **_ask(gate, f"{ack}{question_for(action, still[0], intro=False)}")}

    perms = dict(state.get("user_permissions") or {})
    perms["can_sell" if gate.get("capability") == "SELL" else "can_buy"] = True
    updates["user_permissions"] = perms
    logger.info("profile_gate_completed | goal=%s | action=%s", gate.get("goal"), action.value)

    # Fail-closed : la commande en pause DOIT être exploitable, sinon aucune reprise inventée.
    command = dict(gate.get("command") or {})
    if not command:  # gate sans commande capturée : repli historique (objectif d'origine uniquement)
        command = {"goal": gate.get("goal"), "current_goal": gate.get("goal"), "detected_intent": gate.get("intent"),
                   "interpreted_event": gate.get("event"), "status": gate.get("status")}
        gate["command"] = command
    if not command_is_resumable(command, state):
        logger.error("business_resume_failed | goal=%s | action=%s | reason=invalid_resume_context", gate.get("goal"), action.value)
        return {**updates, "profile_gate": None, "is_onboarding": False, "status": "COMPLETED", "response_strategy": "SUCCESS",
                "final_response": SAFE_RESUME_FAILURE, "ag_ui_component": None}
    logger.info("business_resume_started | goal=%s | action=%s", gate.get("goal"), action.value)
    return build_resume_patch(state, gate, updates)


__all__ = ["SAFE_RESUME_FAILURE", "build_resume_patch", "profile_gate_turn"]
