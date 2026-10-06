"""Un tour de collecte JUSTE-À-TEMPS du profil (« mode gate » de `onboarding_node`).

Ce n'est PAS un nouveau moteur d'onboarding : il réutilise l'extracteur existant (`_llm_extract_onboarding_all`), le
résolveur des 17 régions (`agents/onboarding.py::_resolve_zone`) et l'outil MCP `complete_user_profile`. Il s'exécute
quand `core/profile_gate.py` a posé `profile_gate` (une ACTION a besoin d'un champ manquant) ; l'intention et le payload
de l'action restent dans l'état, INTACTS.

Issues d'un tour :
  - champ(s) reçu(s) mais il en manque encore  -> on pose la question suivante (une à la fois) ;
  - tout est là                               -> REPRISE : l'action reprend dans le MÊME tour (`context_resolver`), sans
                                                 que l'utilisateur répète son besoin ;
  - le message n'est pas une réponse          -> RELÂCHE : l'utilisateur change de sujet, il n'est jamais prisonnier du
                                                 mini-parcours (le message repart vers l'interpréteur).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from ladini.agents.onboarding import _resolve_zone
from ladini.domain.burkina_regions import resolve_region
from ladini.domain.profile_requirements import (
    ProfileAction,
    ProfileFacts,
    ProfileField,
    get_missing_requirements,
    is_real_name,
)
from ladini.graphs.agents.market_coach.core.profile_gate import question_for
from ladini.graphs.agents.market_coach.services.mcp.gateway import ProfileGateway
from ladini.graphs.agents.market_coach.utils import _llm_extract_onboarding_all

logger = logging.getLogger("Ladini.Market.ProfileGateTurn")

_NAME_PREFIX = re.compile(
    r"^(?:bonjour|salut|bonsoir)?[\s,;:!.-]*(?:moi\s+c['’ ]?est|je\s+m['’ ]?appelle|mon\s+nom\s+est|c['’ ]?est|je\s+suis)\s+",
    re.IGNORECASE,
)


def _plain_name(text: str) -> Optional[str]:
    """Repli SANS LLM : une réponse courte, sans chiffre ni point d'interrogation, à « comment t'appelles-tu ? »."""
    cleaned = _NAME_PREFIX.sub("", (text or "").strip()).strip(" .!,;:")
    tokens = cleaned.split()
    # Au plus 3 mots, uniquement des lettres (traits d'union/apostrophes admis DANS un mot) : « Moussa », « Chez Ali »,
    # « Restaurant Wend Konta » — jamais une phrase (« montre-moi d'abord les prix »).
    if not cleaned or len(tokens) > 3 or not all(re.fullmatch(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*", t) for t in tokens):
        return None
    return cleaned


def _facts_after(state: Dict[str, Any], updates: Dict[str, Any]) -> ProfileFacts:
    merged = {**state, **updates}
    return ProfileFacts.from_state(merged)


async def profile_gate_turn(state: Dict[str, Any], mc_runtime: Any) -> Dict[str, Any]:
    gate: Dict[str, Any] = dict(state.get("profile_gate") or {})
    action = ProfileAction(gate.get("action") or ProfileAction.DISCOVERY.value)
    missing = [ProfileField(m) for m in gate.get("missing") or []]
    text = str(state.get("normalized_text") or state.get("user_query") or "").strip()
    phone = str(state.get("user_phone") or "").strip()

    extracted: Dict[str, Any] = {}
    if text:
        try:
            extracted = await _llm_extract_onboarding_all(
                mc_runtime, text, context_hint=f"Information demandée : {', '.join(f.value for f in missing)}."
            )
        except Exception:  # noqa: BLE001 - l'extraction déterministe ci-dessous suffit à avancer
            extracted = {}

    fields: Dict[str, Any] = {}
    state_updates: Dict[str, Any] = {}
    region_question: Optional[str] = None

    # Région : déterministe d'abord (17 régions, chefs-lieux, surnoms) — aucun appel LLM nécessaire.
    if ProfileField.REGION in missing:
        zone_text = str(extracted.get("zone") or "").strip() or text
        hit = resolve_region(zone_text)
        if hit.status == "RESOLVED":
            resolution = await _resolve_zone(zone_text, mc_runtime, allow_clarify=False)
            fields.update(
                zone_id=resolution.zone_id,
                declared_location=resolution.declared or (hit.region.name if hit.region else zone_text),
                coverage=resolution.status,
            )
            state_updates.update(
                zone_id=resolution.zone_id,
                zone_name=resolution.zone_name or (hit.region.name if hit.region else None),
                declared_location=fields["declared_location"],
            )
        elif missing[0] is ProfileField.REGION and not gate.get("region_clarified") and (extracted.get("zone") or hit.status == "AMBIGUOUS"):
            region_question = "Dans quelle région es-tu ? (par exemple Kadiogo, Guiriko, Kuilsé…)"
            gate["region_clarified"] = True

    # Nom d'affichage (personne OU établissement).
    if ProfileField.NAME in missing:
        candidate = str(extracted.get("name") or "").strip()
        # Repli déterministe UNIQUEMENT si le LLM n'a pas répondu : quand il a répondu « pas de nom », on le croit.
        if not candidate and missing[0] is ProfileField.NAME and not extracted.get("_ok"):
            candidate = _plain_name(text) or ""
        if candidate and is_real_name(candidate):
            fields["name"] = candidate
            state_updates["user_name"] = candidate

    if not fields and region_question is None:
        if extracted.get("is_question") and extracted.get("reply"):
            return _ask(gate, f"{extracted['reply']}\n\n{question_for(action, missing[0], intro=False)}")
        logger.info("profile_gate_released | goal=%s | reason=not_an_answer", gate.get("goal"))
        return {"profile_gate": None, "is_onboarding": False, "profile_gate_outcome": "RELEASE"}

    if region_question is not None and not fields:
        return _ask(gate, region_question)

    still = get_missing_requirements(action, _facts_after(state, state_updates))
    if phone:
        try:
            await ProfileGateway(mc_runtime).complete_user_profile(
                phone, **fields, **({"capability": gate.get("capability")} if not still and gate.get("capability") else {})
            )
        except Exception as exc:  # noqa: BLE001 - le profil sera redemandé : jamais silencieusement perdu
            logger.warning("complete_user_profile a échoué : %s", exc)
            return _ask(gate, "Un incident technique m'empêche d'enregistrer cette information. Peux-tu me la redire dans un instant ?")

    if still:
        gate["missing"] = [f.value for f in still]
        ack = f"Merci {state_updates['user_name']} ✅\n\n" if "user_name" in state_updates else ""
        return {**state_updates, **_ask(gate, f"{ack}{question_for(action, still[0], intro=False)}")}

    logger.info("profile_gate_completed | goal=%s | action=%s", gate.get("goal"), action.value)
    perms = dict(state.get("user_permissions") or {})
    if gate.get("capability") == "SELL":
        perms["can_sell"] = True
    else:
        perms["can_buy"] = True
    return {
        **state_updates,
        "user_permissions": perms,
        "profile_gate": None,
        "is_onboarding": False,
        "profile_gate_outcome": "RESUME",
        # L'événement/l'intention/le statut ORIGINAUX de l'action reprennent : le flow redémarre comme au premier passage.
        "interpreted_event": gate.get("event"),
        "detected_intent": gate.get("intent"),
        "status": gate.get("status") or "PLANNING",
    }


def _ask(gate: Dict[str, Any], text: str) -> Dict[str, Any]:
    return {
        "profile_gate": gate,
        "is_onboarding": True,
        "status": "WAITING_INPUT",
        "response_strategy": "ONBOARDING",
        "onboarding_prompt": text,
        "final_response": text,
        "ag_ui_component": None,
    }


__all__ = ["profile_gate_turn"]
