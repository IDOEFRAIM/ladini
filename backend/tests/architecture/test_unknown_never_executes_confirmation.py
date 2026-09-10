"""Test d'architecture (mandat de validation réelle §29/§14) : un tour dont
l'interprétation est UNKNOWN ne doit JAMAIS exécuter l'action métier d'une
CONFIRM_ACTION en attente — même si un ancien `resolved_id`/état périmé
traîne dans le payload. `confirmation_gate.py` est le SEUL exécuteur légitime
pour les goals qui y transitent ; ce test verrouille sa garde CONFIRM
explicite (`event == "CONFIRM"`, jamais un défaut permissif)."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.nodes.confirmation_gate import (
    confirmation_gate,
)
from tests.conftest import make_state, run
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    set_pending_interaction,
)


def _pending_confirmation_state(event: str, **extra) -> dict:
    base = {
        "current_goal": "SALES_UPDATE_PRODUCT",
        "interpreted_event": event,
        "transaction_payload": {"product": "mais", "quantity": 50, "price": 500},
        "confirmation_summary": "Récapitulatif : 50 kg de mais — 25000 FCFA",
        **set_pending_interaction(
            InteractionKind.CONFIRM_ACTION, context_ref="confirmation"
        ),
    }
    base.update(extra)
    return make_state(**base)


class TestUnknownNeverAuthorizesExecution:
    def test_unknown_event_does_not_authorize_execution(self):
        state = _pending_confirmation_state("UNKNOWN")
        result = run(confirmation_gate(state, None))
        assert result.get("execution_authorized") is not True
        assert result.get("is_certified") is not True

    def test_out_of_scope_event_does_not_authorize_execution(self):
        state = _pending_confirmation_state("OUT_OF_SCOPE")
        result = run(confirmation_gate(state, None))
        assert result.get("execution_authorized") is not True

    def test_answer_event_pretending_to_be_a_correction_does_not_authorize_execution(self):
        """Une "correction" (ANSWER) pendant la confirmation ne doit jamais
        se substituer à un CONFIRM explicite — même avec un payload à jour."""
        state = _pending_confirmation_state("ANSWER")
        result = run(confirmation_gate(state, None))
        assert result.get("execution_authorized") is not True

    def test_only_a_real_confirm_event_authorizes_execution(self):
        """Contrôle positif — le seul chemin qui DOIT autoriser l'exécution."""
        state = _pending_confirmation_state("CONFIRM")
        result = run(confirmation_gate(state, None))
        assert result["execution_authorized"] is True
        assert result["is_certified"] is True

    def test_a_stale_resolved_id_in_the_payload_never_substitutes_for_the_current_event(self):
        """Défense en profondeur : même si `transaction_payload` contient un
        vieux `resolved_id`/indice d'une confirmation passée, seul
        `interpreted_event` du TOUR COURANT décide — jamais une donnée
        résiduelle du payload (voir mandat de validation §30, "stale outcome
        isolation")."""
        state = _pending_confirmation_state(
            "UNKNOWN",
            transaction_payload={
                "product": "mais",
                "quantity": 50,
                "price": 500,
                "resolved_id": "PREORDER_CONFIRM",
            },
        )
        result = run(confirmation_gate(state, None))
        assert result.get("execution_authorized") is not True
