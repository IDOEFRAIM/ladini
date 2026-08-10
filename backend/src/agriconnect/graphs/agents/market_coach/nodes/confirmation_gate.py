import time
from typing import Any, Dict

from agriconnect.graphs.agents.market_coach.core.base import get_node_logger, _READ_GOALS
from agriconnect.graphs.agents.market_coach.services.ui.confirmation_summary import (
    build_confirmation_summary as _build_confirmation_summary,
)

logger = get_node_logger("ConfirmationGateNode")

# Goals où un REJECT pendant la confirmation ne doit PAS effacer le brouillon
# (produit/quantité/prix déjà saisis) — l'utilisateur doit pouvoir corriger un
# champ (ex: "non" puis "plafond 400") sans tout reprendre depuis zéro. Bug
# vécu en prod : un producteur voulait juste corriger le prix plafond d'un
# appel d'offres, a répondu "non" au récap, s'est retrouvé traité comme un
# inconnu (goal effacé, plus aucun contexte) dès le message suivant.
#
# Contrairement aux tunnels `producer_update` (flows/producer/flow.py), qui
# contournent ENTIÈREMENT ce nœud pour gérer leur propre cycle CONFIRM/REJECT/
# correction — y compris l'écriture en base, appelée directement par le flow —
# cette liste ne touche QUE le REJECT : le chemin CONFIRM (et donc l'écriture
# réelle, ex. `create_auction`) continue de passer par ce nœud puis l'exécuteur
# générique, inchangé. Sûr par construction : aucune logique d'écriture n'est
# dupliquée ici.
_DRAFT_PRESERVING_REJECT_GOALS = frozenset({"PROCUREMENT_CREATE_REQUEST"})

# Au-delà de ce délai sans réponse claire (oui/non), une confirmation en
# attente est considérée abandonnée plutôt que ré-affichée indéfiniment sur
# un message sans rapport arrivé bien plus tard (ex: un partage de position
# GPS reçu 30 min après une confirmation jamais tranchée). Purement temporel
# — aucune heuristique sur le CONTENU du message.
_CONFIRMATION_TTL_SECONDS = 600.0

_ABANDON_PATCH: Dict[str, Any] = {
    "is_certified": False,
    "execution_authorized": False,
    "waiting_for_confirmation": False,
    "confirmation_summary": None,
    "confirmation_raised_at": None,
    "current_goal": None,
    "transaction_payload": {"__reset__": True},
    "missing_fields": [],
    "completed_fields": [],
    "expected_input": "NONE",
    "goal_status": "IDLE",
    "status": "COMPLETED",
    "response_strategy": None,
    "ag_ui_component": None,
}


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

    awaiting_confirmation = bool(state.get("waiting_for_confirmation")) or (
        str(state.get("expected_input") or "").upper() == "CONFIRMATION"
    )
    if awaiting_confirmation:
        if event == "CONFIRM":
            return {
                "is_certified": True,
                "execution_authorized": True,
                "waiting_for_confirmation": False,
                "confirmation_raised_at": None,
                "status": "EXECUTING",
                "ag_ui_component": None,
            }
        if event == "REJECT":
            if goal in _DRAFT_PRESERVING_REJECT_GOALS:
                # Repli DOUX : n'efface QUE l'état de confirmation (résumé,
                # drapeau d'attente) — `current_goal`/`transaction_payload`/
                # `active_form`/`form_data` survivent intacts. Le tour suivant
                # retombe naturellement sur le flow dédié (ex:
                # `buyer_request_resolver`, ré-entrée déjà existante sur
                # `active_form == "AUCTION_CREATE"`), qui ré-affichera un récap
                # à jour si l'utilisateur fournit une correction — ou, si
                # l'utilisateur répond "non"/"annuler" une SECONDE fois sans
                # rien d'autre, `goal_planner` applique déjà son repli
                # générique complet (RÈGLE 1, hors confirmation) : rien à
                # dupliquer ici pour ce cas.
                logger.info(
                    "[ConfirmationGate] REJECT sur %s — brouillon préservé (correction possible au tour suivant)",
                    goal,
                )
                return {
                    "is_certified": False,
                    "execution_authorized": False,
                    "waiting_for_confirmation": False,
                    "confirmation_raised_at": None,
                    "confirmation_summary": None,
                    "expected_input": "NONE",
                    "goal_status": "ACTIVE",
                    "status": "PLANNING",
                    "response_strategy": "SUCCESS",
                    "final_response": (
                        "D'accord, ce n'est pas encore confirmé. Dites-moi ce que "
                        "vous voulez modifier (prix, quantité, date), ou répondez "
                        "*annuler* pour abandonner."
                    ),
                    "ag_ui_component": None,
                }
            return {
                "is_certified": False,
                "execution_authorized": False,
                "waiting_for_confirmation": False,
                "confirmation_raised_at": None,
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

        # Ni CONFIRM ni REJECT : soit une correction (gérée en amont par les
        # flows dédiés avant d'atteindre ce nœud), soit un message sans
        # rapport (ex: partage GPS, changement de sujet). Résilience : si la
        # confirmation traîne trop longtemps (péremption) OU si le goal
        # associé est introuvable (état orphelin — ne devrait plus arriver
        # depuis la préservation de `current_goal` par post_response_cleanup,
        # mais on se protège quand même), on abandonne silencieusement au
        # lieu de ré-afficher un récap confus voire vide.
        raised_at = state.get("confirmation_raised_at")
        is_stale = bool(raised_at) and (time.time() - float(raised_at)) > _CONFIRMATION_TTL_SECONDS
        is_orphaned = not goal or not payload
        if is_stale or is_orphaned:
            logger.info(
                "[ConfirmationGate] Confirmation abandonnée (stale=%s, orphaned=%s, goal=%r)",
                is_stale, is_orphaned, goal,
            )
            return dict(_ABANDON_PATCH)

    # Garde-fou symétrique à celui du ré-affichage ci-dessus : ne JAMAIS lever
    # une confirmation sans goal ni payload — ça ne peut produire qu'un récap
    # vide/incompréhensible ("Validation de l'opération : " sans rien après).
    # Un routage a mal résolu le goal courant AVANT d'arriver ici (bug amont) ;
    # mieux vaut abandonner proprement que d'exposer l'état interne cassé.
    if not goal or not payload:
        logger.warning(
            "[ConfirmationGate] Appel avec goal/payload vide — abandon au lieu d'un récap creux (goal=%r, payload_keys=%s)",
            goal, list(payload.keys()),
        )
        return dict(_ABANDON_PATCH)

    summary = _build_confirmation_summary(goal, payload)
    return {
        "waiting_for_confirmation": True,
        "is_certified": False,
        "execution_authorized": False,
        "confirmation_summary": summary,
        "confirmation_raised_at": state.get("confirmation_raised_at") or time.time(),
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
