"""`nodes/clarification.py` — SEUL endroit du graphe qui commente un résultat
GPS quand aucune étape GPS n'est activement en attente (`gps_delivery_gate.py`
couvre l'autre cas). Voir `tests/unit/test_webhook_gps_single_response.py`
pour la preuve jumelle côté webhook (qui, lui, n'envoie plus jamais rien)."""
from __future__ import annotations

from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    set_pending_interaction,
)
from agriconnect.graphs.agents.market_coach.nodes.clarification import (
    clarification_node,
)
from tests.conftest import make_state, run


class TestLocationOutcomeWithNoActiveGpsStage:
    def test_out_of_zone_with_no_active_flow_produces_the_single_response(self):
        state = make_state(
            interpreted_event="UNKNOWN",
            current_goal=None,
            location_shared=True,
            location_outcome="LOCATION_OUT_OF_ZONE",
        )
        result = run(clarification_node(state, None))
        assert result.get("response_strategy") == "CLARIFICATION"
        assert "hors de notre zone" in result.get("final_response", "")

    def test_persistence_error_with_no_active_flow_produces_the_single_response(self):
        state = make_state(
            interpreted_event="UNKNOWN",
            current_goal=None,
            location_shared=True,
            location_outcome="LOCATION_PERSISTENCE_ERROR",
        )
        result = run(clarification_node(state, None))
        assert result.get("response_strategy") == "CLARIFICATION"
        assert "souci technique" in result.get("final_response", "")

    def test_out_of_zone_does_not_disturb_an_unrelated_active_tunnel(self):
        """Le message est produit, mais current_goal/transaction_payload ne
        sont JAMAIS touchés — l'utilisateur retrouve sa place au tour
        suivant dans le tunnel où il était (voir post_response_cleanup)."""
        state = make_state(
            interpreted_event="UNKNOWN",
            current_goal="SALES_PUBLISH_PRODUCT",
            transaction_payload={"product": "mais", "quantity": 50},
            location_shared=True,
            location_outcome="LOCATION_OUT_OF_ZONE",
        )
        result = run(clarification_node(state, None))
        assert result.get("response_strategy") == "CLARIFICATION"
        assert "current_goal" not in result
        assert "transaction_payload" not in result

    def test_an_active_provide_location_stage_is_left_to_gps_delivery_gate(self):
        """Le garde-fou central : si une étape GPS EST active, ce nœud ne
        doit RIEN produire pour ce résultat — c'est `resolve_gps_stage` plus
        bas dans le graphe qui doit s'en charger, seul."""
        state = make_state(
            interpreted_event="UNKNOWN",
            current_goal="BUYER_PREORDER_INIT",
            location_shared=True,
            location_outcome="LOCATION_OUT_OF_ZONE",
            **set_pending_interaction(
                InteractionKind.PROVIDE_LOCATION, context_ref="confirmation"
            ),
        )
        result = run(clarification_node(state, None))
        assert "final_response" not in result or not result.get("final_response")
        assert result.get("response_strategy") != "CLARIFICATION"

    def test_a_new_accepted_location_is_silent_here_too(self):
        """Un point ACCEPTÉ n'a rien de spécial à dire ici — c'est un succès
        silencieux (le tour continue normalement)."""
        state = make_state(
            interpreted_event="UNKNOWN",
            current_goal=None,
            location_shared=True,
            location_outcome="NEW_LOCATION_ACCEPTED",
        )
        result = run(clarification_node(state, None))
        assert result == {}
