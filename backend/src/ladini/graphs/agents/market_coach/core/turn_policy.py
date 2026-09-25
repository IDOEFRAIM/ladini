"""`decide_turn` — vocabulaire canonique d'un tour, en mode SHADOW (Phase 2 hardening).

Aujourd'hui, la décision « ce tour est-il une nouvelle tâche, une continuation, une
interruption, une correction... ? » est répartie sur 3 couches qui écrivent chacune leur
propre vocabulaire (`interpreted_event` de l'interpréteur, réécrit ensuite par lui-même ;
`cognitive_decision.action` de `cognitive_guard` ; les branches RULE 1-5 de `goal_planner`).
L'audit (`docs/CONVERSATIONAL_ENGINE_HARDENING_AUDIT_2026-09-24.md`, §4) recommande une
politique de tour UNIQUE, mais explicitement introduite d'abord en mode SHADOW : mesurer,
pas remplacer, avant de rendre quoi que ce soit autoritaire (mandat Phase 2 §21-22 : « Ne
bascule pas tout d'un coup »).

`classify_turn` ne DÉCIDE donc RIEN — elle ÉTIQUETTE, dans le vocabulaire canonique cible,
ce que les couches existantes ont DÉJÀ décidé (`cognitive_decision`/`interpreted_event`/la
transition de goal réellement appliquée par `goal_planner`). Aucune couche ne consomme
encore son résultat pour router : c'est une fonction pure d'OBSERVABILITÉ, branchée en
commit 11 par `core/turn_trace.py::capture_pre_cleanup` (voir sa docstring de module), qui
rend visible, tour après tour, comment le comportement RÉEL se répartit dans ce
vocabulaire — la matrice nécessaire avant qu'un futur chantier puisse envisager de le
rendre autoritaire.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from ladini.graphs.agents.market_coach.core.conversation_decision import (
    ConversationAction,
)


class TurnAction(str, Enum):
    """Vocabulaire canonique cible (audit §3, mandat Phase 2 §21) — un sur-ensemble
    couvrant à la fois les événements de l'interpréteur et les décisions de
    `cognitive_guard`, unifiés."""

    NEW_TASK = "NEW_TASK"
    CONTINUE = "CONTINUE"
    INTERRUPT = "INTERRUPT"
    ANSWER_PENDING = "ANSWER_PENDING"
    CORRECT = "CORRECT"
    CONFIRM = "CONFIRM"
    REJECT = "REJECT"
    CANCEL = "CANCEL"
    CLARIFY = "CLARIFY"
    UNKNOWN = "UNKNOWN"


#: Phrases déterministes déjà reconnues comme une annulation EXPLICITE ailleurs dans le
#: moteur (`flows/buyer/recurring_need.py::_ABANDON_PHRASES`) — reprises ici à l'IDENTIQUE
#: (même liste, pas une seconde copie) pour distinguer CANCEL de REJECT dans l'étiquette.
_CANCEL_PHRASES = frozenset({"laisse tomber", "laisse", "annule", "annuler", "oublie ça", "oublie ca", "stop"})


@dataclass(frozen=True)
class TurnClassification:
    action: TurnAction
    reason: str


def classify_turn(
    *,
    interpreted_event: Optional[str],
    cognitive_decision: Optional[Dict[str, Any]],
    goal_before: Optional[str],
    goal_after: Optional[str],
    normalized_text: str = "",
) -> TurnClassification:
    """Étiquette CE tour dans le vocabulaire canonique, à partir de ce que les couches
    existantes ont déjà produit — jamais un recalcul indépendant. Voir la docstring de
    module : shadow uniquement, aucun consommateur autoritaire aujourd'hui."""
    event = str(interpreted_event or "UNKNOWN").upper().strip()
    action = str((cognitive_decision or {}).get("action") or "").strip()
    bare = normalized_text.strip().lower().strip(" .!?,;:")

    if action == ConversationAction.INTERRUPT_ACTIVE_GOAL:
        return TurnClassification(TurnAction.INTERRUPT, "cognitive_guard_approved")

    if event == "CONFIRM":
        return TurnClassification(TurnAction.CONFIRM, "confirm_event")

    if event == "REJECT":
        if bare in _CANCEL_PHRASES:
            return TurnClassification(TurnAction.CANCEL, "reject_bare_cancel_phrase")
        return TurnClassification(TurnAction.REJECT, "reject_event")

    if action in (ConversationAction.RECOVER_ACTIVE_GOAL, ConversationAction.ABANDON_ACTIVE_GOAL):
        return TurnClassification(TurnAction.CLARIFY, f"cognitive_guard:{action}")

    if action == ConversationAction.DISAMBIGUATE:
        return TurnClassification(TurnAction.CLARIFY, "cognitive_guard_disambiguate")

    if action == ConversationAction.CLARIFY:
        return TurnClassification(TurnAction.CLARIFY, "cognitive_guard_clarify")

    if event in {"UPDATE", "ANSWER"} and goal_before and goal_before == goal_after:
        return TurnClassification(TurnAction.ANSWER_PENDING, f"{event.lower()}_same_goal")

    if event == "NEW_TASK":
        if goal_before and goal_before == goal_after:
            # Même goal : soit une simple continuation, soit une correction du draft en
            # cours (le domaine décide laquelle — voir `flows/*/*.py::_is_correction`) ;
            # cette classification NE tranche PAS entre les deux, elle documente que
            # `cognitive_guard` n'a PAS jugé nécessaire d'interrompre.
            return TurnClassification(TurnAction.CORRECT, "new_task_same_goal_not_interrupted")
        if not goal_before:
            return TurnClassification(TurnAction.NEW_TASK, "new_task_no_prior_goal")
        if goal_before != goal_after:
            return TurnClassification(TurnAction.NEW_TASK, "new_task_goal_switched")
        return TurnClassification(TurnAction.CONTINUE, "new_task_goal_unchanged")

    if event in {"UPDATE", "ANSWER"} and goal_before:
        return TurnClassification(TurnAction.CONTINUE, f"{event.lower()}_continuation")

    if event in {"UNKNOWN", "OUT_OF_SCOPE"}:
        return TurnClassification(TurnAction.CLARIFY, event.lower())

    return TurnClassification(TurnAction.UNKNOWN, f"unclassified:{event}")


__all__ = ["TurnAction", "TurnClassification", "classify_turn"]
