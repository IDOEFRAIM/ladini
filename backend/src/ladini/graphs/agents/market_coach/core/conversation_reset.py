"""Réinitialisation d'un tunnel conversationnel abandonné.

Extrait de `nodes/cognitive.py::cognitive_guard` (2026-09-08, refonte
responsabilités des nœuds d'entrée, mandat §7) : ce nœud ne doit plus être
le propriétaire de connaissance détaillée des mini-machines à états
transactionnelles (`BidWorkflowState`, `ProducerUpdateWorkflowState`,
`WinnerGpsWorkflowState`, `preorder_workflow`) ni du nettoyage de
`transaction_payload`/`selected_tool`/`execution_result`. Ces
responsabilités n'ont pas de propriétaire sûr et audité à ce jour (les
flows producteur/acheteur qui possèdent ces contrats n'ont pas encore été
inspectés dans ce chantier) — encapsulées ICI, dans un helper nommé et
documenté, plutôt que réinventées à la volée dans `cognitive_guard`
(mandat §7 : "encapsuler temporairement le comportement derrière un
helper dédié plutôt que d'inventer une nouvelle architecture sur les
nœuds non audités").

`cognitive_guard` reste l'appelant (c'est lui qui DÉCIDE de l'abandon,
sur dépassement de `retry_count`) ; ce module ne fait qu'exécuter la
réinitialisation une fois la décision prise.
"""

from __future__ import annotations

from typing import Any, Dict

from ladini.graphs.agents.market_coach.core.conversation_decision import (
    ConversationAction,
)
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    clear_pending_interaction,
)
from ladini.graphs.agents.market_coach.flows.buyer.contexts import (
    WinnerGpsWorkflowState,
)
from ladini.graphs.agents.market_coach.flows.producer.contexts import (
    BidWorkflowState,
    ProducerUpdateWorkflowState,
)


def reset_abandoned_conversation_context(
    state: Dict[str, Any],
    *,
    intent_competition: Any,
    cognitive_decision: Dict[str, Any],
) -> Dict[str, Any]:
    """Construit le patch complet d'abandon de tunnel (max retries atteint).

    Réinitialise : le goal courant, les mini-machines à états
    auto-suffisantes des flows producteur/acheteur, le brouillon de
    transaction, l'interaction en attente, et pose une réponse de
    clarification neutre. Voir l'historique de `cognitive_guard` pour la
    genèse de chaque champ réinitialisé — préservé ici tel quel, aucun
    comportement n'a changé, seule l'organisation du code a bougé.
    """
    stale_wm_keys = (
        BidWorkflowState.KEYS
        | ProducerUpdateWorkflowState.KEYS
        | WinnerGpsWorkflowState.KEYS
    )
    working_memory = dict(state.get("working_memory") or {})
    for key in stale_wm_keys:
        working_memory[key] = None

    return {
        "current_goal": None,
        "goal_status": "IDLE",
        "status": "WAITING_INPUT",
        "preorder_workflow": {"gps_stage": None, "gps_default": None},
        # merge_dict fields: must use the reset sentinel, a plain {} is a
        # no-op under merge_dict semantics (agents/reducers.py).
        "transaction_payload": {"__reset__": True},
        "stable_entities": {"__reset__": True},
        "missing_fields": [],
        "completed_fields": [],
        "last_missing_field": None,
        "expected_candidates": [],
        "available_mapping": {},
        "retry_count": 0,
        "confirmation_summary": None,
        **clear_pending_interaction("tunnel_abandoned_max_retries"),
        "selected_tool": None,
        "selected_tool_args": {"__reset__": True},
        "execution_result": {"__reset__": True},
        "ag_ui_component": None,
        "response_strategy": "CLARIFICATION",
        "intent_competition": intent_competition,
        "cognitive_decision": {
            **cognitive_decision,
            "action": ConversationAction.ABANDON_ACTIVE_GOAL,
        },
        "proactive_hint": "L'opération a été annulée. Dites-moi ce que vous souhaitez faire.",
        "working_memory": working_memory,
    }


__all__ = ["reset_abandoned_conversation_context"]
