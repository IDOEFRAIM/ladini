"""`decide_turn` — vocabulaire canonique d'un tour. SHADOW pour `classify_turn`
(observabilité, Phase 2), AUTORITAIRE pour `decide_active_draft_reply` UNIQUEMENT
(Phase 2.5, H5/H7 — voir sa propre docstring pour la frontière exacte).

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

## Phase 2.5 — `decide_active_draft_reply` devient AUTORITAIRE, sur UNE frontière étroite

H5/H7 (mandat Phase 2.5, 2026-09-25) partageaient un invariant manquant commun : la
question « ce message reçu pendant la confirmation/correction d'un draft actif CORRIGE-t-il
ce draft, ou démarre-t-il une tâche INDÉPENDANTE ? » était re-décidée localement, deux fois,
par deux heuristiques narrow et incomplètes — `interpreter/routing.py::_interpret_fast_path`
(H5, un simple nombre typé pendant une CONFIRMATION) et `flows/buyer/recurring_need.py::
_is_correction` (H7, l'absence de `recurrence_type`, aveugle à la confiance et à l'intent
détecté). Aucune des deux ne voyait le contexte complet. `decide_active_draft_reply`
centralise cette UNE question — rien de plus : elle ne décide PAS l'interruption
(`cognitive_guard` en reste seul propriétaire), ne décide PAS le routage du goal
(`goal_planner`), seulement CORRECT vs NEW_TASK vs CLARIFY pour un message qui a DÉJÀ
traversé l'interpréteur réel (jamais le fast-path pré-LLM, désormais désactivé pour ce cas
précis — voir `_interpret_fast_path`) et que `cognitive_guard` a DÉJÀ laissé passer sans
interrompre. Un seul appelant aujourd'hui (`recurring_need.py`) — garde architecturale
dans `tests/architecture/test_turn_policy_authoritative_boundary.py`.
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


def decide_active_draft_reply(
    *,
    interpreted_event: Optional[str],
    said_entities: Optional[Dict[str, Any]],
    detected_intent: Optional[str],
    current_goal: Optional[str],
) -> TurnAction:
    """SEUL point d'entrée AUTORITAIRE de ce module (voir docstring de module,
    section Phase 2.5) — appelé une fois que `cognitive_guard` a déjà laissé
    passer le tour sans interrompre le goal actif, pour trancher CE tour entre
    CORRECT (le draft actif absorbe `said_entities`), NEW_TASK (le draft actif
    reste intact, une tâche indépendante démarre ailleurs) et CLARIFY (ni l'un
    ni l'autre n'est assez sûr — ne jamais deviner, cf. mandat Phase 2.5 §10).

    N'invente rien : elle relit `interpreted_event`/`said_entities` produits
    par l'interpréteur RÉEL (jamais le fast-path pré-LLM — désactivé pour ce
    cas, voir `_interpret_fast_path`) et le `detected_intent` qu'il a extrait,
    contre le `current_goal` déjà résolu par l'appelant. Un signal fort et déjà
    existant qu'une tâche est isolée et complète — `recurrence_type` présent
    dans `said_entities` — l'emporte : mandat §9 cas (E)/(F) (« je veux 30
    poulets chaque semaine » pendant une confirmation ne doit jamais voler son
    nombre au draft en cours). Sans ce signal, seule une divergence claire entre
    l'intent détecté et le goal actif justifie CLARIFY plutôt que CORRECT —
    l'ambiguïté ne doit jamais se résoudre par une mutation silencieuse.
    """
    event = str(interpreted_event or "").upper().strip()

    # Un REJECT/NEW_TASK/INTERRUPTION SANS aucune valeur dite ce tour n'a rien
    # à corriger — même filet que l'ancien `_is_correction` (« if not said:
    # return False »), reproduit ici à l'identique avant toute nouvelle logique.
    if event == "REJECT":
        return TurnAction.CORRECT if said_entities else TurnAction.REJECT

    # Le classifieur n'a dégagé AUCUNE intention exploitable (UNKNOWN — pas
    # "une intention différente", juste "aucune"), mais CE message porte
    # quand même une valeur structurée alors qu'une question précise est en
    # attente sur CE draft (`_pending_targets`, garanti par l'appelant). Une
    # quantité/unité isolée, sans produit ni intention propre, ne peut
    # structurellement PAS démarrer une tâche indépendante — elle ne peut
    # que répondre à la question posée. Filet de dernier recours,
    # symétrique au chemin legacy (`interpreter/entities.py::
    # _fallback_quantity_unit_from_text`), pour le pire cas où même le LLM
    # échoue à classifier (voir `TestRealCorrectionUpdatesTheSameDraft`).
    if event == "UNKNOWN" and said_entities:
        return TurnAction.CORRECT

    if event not in ("NEW_TASK", "INTERRUPTION"):
        return TurnAction.ANSWER_PENDING

    if not said_entities or said_entities.get("recurrence_type"):
        return TurnAction.NEW_TASK

    intent_up = str(detected_intent or "").upper().strip()
    goal_up = str(current_goal or "").upper().strip()
    if intent_up and intent_up != "UNKNOWN" and goal_up and intent_up != goal_up:
        return TurnAction.CLARIFY

    return TurnAction.CORRECT


__all__ = [
    "TurnAction",
    "TurnClassification",
    "classify_turn",
    "decide_active_draft_reply",
]
