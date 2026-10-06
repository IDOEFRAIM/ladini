"""Collecte JUSTE-À-TEMPS du profil — le seul point où Ladini décide qu'une information manque.

Principe : Ladini comprend d'abord l'intention ; il ne demande une information personnelle que si l'ACTION métier
courante l'exige (`domain/profile_requirements.py` : découverte = rien, besoin d'achat = région, engagement/vente = nom +
région). Ce module est la couche mince entre ce contrat PUR et le graphe — il n'ajoute NI nouveau routeur NI nouveau
moteur d'onboarding : la collecte réutilise `flows/common/onboarding.py` (extraction nom/région, résolution des 17
régions) en « mode gate », et la reprise réutilise `context_resolver`.

`apply_profile_gate` est appelé par `DomainRouter.resolve` (point unique avant tout flow d'acheteur/producteur) :

    objectif courant ──► action ──► champs manquants ?
                                       │ oui : pose `profile_gate` (l'intention + le payload restent INTACTS), explique
                                       │       pourquoi, pose UNE question
                                       └ non : ne fait rien (utilisateur déjà complet = aucune friction nouvelle)

Les capacités sont cumulatives : vendre ajoute la ligne `Producer` (statut `PENDING`, jamais « vérifié ») sans toucher à
un éventuel profil acheteur ; la vérification producteur n'est JAMAIS contournée.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional, Tuple

from ladini.core.settings import settings
from ladini.domain.burkina_regions import resolve_region
from ladini.domain.profile_requirements import (
    ProfileAction,
    ProfileFacts,
    ProfileField,
    action_for_goal,
    get_missing_requirements,
)
from ladini.graphs.agents.market_coach.core.state import resolve_current_goal

logger = logging.getLogger("Ladini.Market.ProfileGate")

#: Ce que l'on demande, en français naturel, une question à la fois.
_QUESTIONS: Mapping[Tuple[ProfileAction, ProfileField], str] = {
    (ProfileAction.SELL, ProfileField.NAME): (
        "Très bien. Avant de publier tes produits, j'ai besoin de quelques informations pour identifier ton "
        "exploitation et sécuriser les échanges avec les acheteurs.\n\nComment t'appelles-tu ?"
    ),
    (ProfileAction.SELL, ProfileField.REGION): "Dans quelle région es-tu principalement basé ?",
    (ProfileAction.BUY_COMMIT, ProfileField.NAME): (
        "J'ai ce qu'il faut pour continuer. Avant de confirmer avec le producteur, quel nom dois-je utiliser "
        "pour toi ou ton établissement ?"
    ),
    (ProfileAction.BUY_COMMIT, ProfileField.REGION): "Dans quelle région souhaites-tu être livré ?",
    (ProfileAction.BUY_REQUEST, ProfileField.REGION): "Dans quelle région souhaites-tu être livré ?",
}
_DEFAULT_QUESTIONS: Mapping[ProfileField, str] = {
    ProfileField.NAME: "Comment dois-je t'appeler ?",
    ProfileField.REGION: "Dans quelle région es-tu ?",
}


def question_for(action: ProfileAction, field: ProfileField, *, intro: bool = True) -> str:
    """Question pour UN champ. `intro=False` : seulement la question (la 2ᵉ du même tour de collecte)."""
    text = _QUESTIONS.get((action, field)) or _DEFAULT_QUESTIONS.get(field, "")
    if not intro and "\n\n" in text:
        text = text.rsplit("\n\n", 1)[1]
    return text


def is_enabled() -> bool:
    return bool(settings.PROGRESSIVE_ONBOARDING_ENABLED)


def gate_requirements(state: Mapping[str, Any]) -> Tuple[ProfileAction, Tuple[ProfileField, ...]]:
    """(action, champs manquants) pour l'objectif courant — pur, sans effet."""
    goal = resolve_current_goal(dict(state))
    action = action_for_goal(goal)
    return action, get_missing_requirements(action, ProfileFacts.from_state(state))


async def _learn_region_from_request(state: Dict[str, Any], mc_runtime: Any) -> Optional[Dict[str, Any]]:
    """Si la demande en cours cite UNE région non ambiguë, l'enregistre au profil (région canonique) et renvoie les faits mis à jour."""
    raw_payload = state.get("transaction_payload")
    raw_entities = state.get("extracted_entities")
    payload: Mapping[str, Any] = raw_payload if isinstance(raw_payload, Mapping) else {}
    entities: Mapping[str, Any] = raw_entities if isinstance(raw_entities, Mapping) else {}
    raw = payload.get("zone") or payload.get("region") or entities.get("zone") or entities.get("region")
    if not raw:
        return None
    hit = resolve_region(raw)
    phone = str(state.get("user_phone") or "").strip()
    if hit.status != "RESOLVED" or hit.region is None or not phone:
        return None
    from ladini.graphs.agents.market_coach.services.mcp.gateway import ProfileGateway

    try:
        await ProfileGateway(mc_runtime).complete_user_profile(phone, declared_location=hit.region.name, coverage="COVERED")
    except Exception as exc:  # noqa: BLE001 - on la redemandera : jamais une région inventée
        logger.warning("Région du message non enregistrée : %s", exc)
        return None
    logger.info("region_learned_from_request | region=%s", hit.region.slug)
    return {"declared_location": hit.region.name}


def needs_producer_capability(state: Mapping[str, Any], action: ProfileAction) -> bool:
    """`True` si l'utilisateur veut VENDRE alors que le profil dit explicitement qu'il n'est pas (encore) producteur.

    Les permissions ne sont jugées que si le profil les a RÉELLEMENT fournies (`user_permissions`) : jamais « pas de
    permissions connues » pris pour « pas producteur »."""
    perms = state.get("user_permissions")
    return action is ProfileAction.SELL and isinstance(perms, Mapping) and not perms.get("can_sell")


def build_gate(state: Mapping[str, Any], action: ProfileAction, missing: Tuple[ProfileField, ...]) -> Dict[str, Any]:
    """Snapshot durable de la commande EN ATTENTE : de quoi la reprendre sans que l'utilisateur la répète."""
    return {
        "goal": resolve_current_goal(dict(state)),
        "action": action.value,
        "missing": [f.value for f in missing],
        "event": state.get("interpreted_event"),
        "intent": state.get("detected_intent"),
        "status": state.get("status"),
        "capability": "SELL" if action is ProfileAction.SELL else "BUY",
    }


async def apply_profile_gate(state: Dict[str, Any], mc_runtime: Any) -> Optional[Dict[str, Any]]:
    """Patch d'état « il manque X » (jamais une exécution), ou `None` quand l'action peut continuer.

    Appelé AVANT le flow du domaine. Si rien ne manque mais qu'un acheteur devient producteur, la capacité est ajoutée en
    silence (aucune question inutile)."""
    if not is_enabled() or state.get("is_onboarding") or state.get("profile_gate"):
        return None
    if not isinstance(state.get("user_permissions"), Mapping):
        # Faits de profil INCONNUS (le profil chargé ne les a pas fournis) : on ne juge jamais « manquant » ce qu'on ne
        # sait pas — un vrai profil (`_serialize_user_entities`) les porte TOUJOURS.
        return None
    action, missing = gate_requirements(state)
    if ProfileField.REGION in missing:
        # Région DÉJÀ dite avec la demande (« …de tomates à Bobo ») : on la retient et on ne la redemande pas.
        learned = await _learn_region_from_request(state, mc_runtime)
        if learned:
            state.update(learned)
            action, missing = gate_requirements(state)
    if missing:
        gate = build_gate(state, action, missing)
        logger.info(
            "profile_gate_triggered | goal=%s | action=%s | missing_requirement=%s | capability_requested=%s",
            gate["goal"], action.value, gate["missing"], gate["capability"],
        )
        return {
            "profile_gate": gate,
            "is_onboarding": True,
            "status": "WAITING_INPUT",
            "response_strategy": "ONBOARDING",
            # `render_onboarding` (stratégie ONBOARDING) lit `onboarding_prompt`, pas `final_response`.
            "onboarding_prompt": question_for(action, missing[0]),
            "final_response": question_for(action, missing[0]),
            "ag_ui_component": None,
        }
    if needs_producer_capability(state, action):
        phone = state.get("user_phone")
        if phone:
            from ladini.graphs.agents.market_coach.services.mcp.gateway import (
                ProfileGateway,
            )

            try:
                await ProfileGateway(mc_runtime).complete_user_profile(str(phone), capability="SELL")
                perms = dict(state.get("user_permissions") or {})
                perms["can_sell"] = True
                state["user_permissions"] = perms
                logger.info("capability_requested | capability=SELL | added=true")
                return {"user_permissions": perms}
            except Exception as exc:  # noqa: BLE001 - le flow rendra l'erreur métier habituelle
                logger.warning("Ajout de la capacité SELL impossible : %s", exc)
    return None


__all__ = [
    "apply_profile_gate",
    "build_gate",
    "gate_requirements",
    "is_enabled",
    "needs_producer_capability",
    "question_for",
]
