"""`nodes/confirmation_gate.py` — décision d'approbation avant écriture DB.

Priorité à la régression production (2026-08) : un REJECT ("non") pendant la
confirmation d'un appel d'offres (PROCUREMENT_CREATE_REQUEST) effaçait tout
le brouillon (produit/quantité/prix), empêchant toute correction — le tour
suivant ("plafond 400") arrivait sans contexte et tombait sur le message
générique "je n'ai pas bien saisi". Le fix est scopé : SEUL le REJECT change
de comportement pour ce goal ; CONFIRM (et donc l'écriture réelle via
l'exécuteur générique) est totalement inchangé.
"""
from __future__ import annotations

import pytest

from agriconnect.graphs.agents.market_coach.nodes.confirmation_gate import confirmation_gate
from tests.conftest import make_state, run


def gate(**overrides):
    return run(confirmation_gate(make_state(**overrides), None))


# =====================================================================
# REJECT — comportement générique (goals NON dans l'allowlist)
# =====================================================================

class TestGenericRejectStillFullyResets:
    def test_reject_during_confirmation_wipes_the_goal_for_ordinary_goals(self):
        """Comportement inchangé pour tout goal hors de l'allowlist — ne pas
        régresser silencieusement les nombreux autres flows qui dépendent de
        ce reset total sur REJECT (vente, panier, stock...)."""
        result = gate(
            current_goal="SALES_PUBLISH_PRODUCT",
            transaction_payload={"product": "mais", "price": 250, "quantity": 100},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            interpreted_event="REJECT",
        )
        assert result["current_goal"] is None
        assert result["transaction_payload"] == {"__reset__": True}
        assert result["status"] == "COMPLETED"
        assert result["response_strategy"] == "CLARIFICATION"

    def test_stock_goal_reject_also_fully_resets(self):
        result = gate(
            current_goal="STOCK_ADJUST",
            transaction_payload={"stock_id": "s1", "quantity": 10},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            interpreted_event="REJECT",
        )
        assert result["current_goal"] is None
        assert result["transaction_payload"] == {"__reset__": True}


# =====================================================================
# REJECT — PROCUREMENT_CREATE_REQUEST (brouillon préservé)
# =====================================================================

class TestProcurementRejectPreservesTheDraft:
    def test_reject_does_not_clear_current_goal(self):
        result = gate(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            transaction_payload={"product": "carottes", "price": 300, "quantity": 500},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            interpreted_event="REJECT",
        )
        assert "current_goal" not in result, "ne doit pas être touché — préservé via merge_dict"

    def test_reject_does_not_reset_transaction_payload(self):
        result = gate(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            transaction_payload={"product": "carottes", "price": 300, "quantity": 500},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            interpreted_event="REJECT",
        )
        assert "transaction_payload" not in result

    def test_reject_clears_only_confirmation_bookkeeping(self):
        result = gate(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            transaction_payload={"product": "carottes", "price": 300, "quantity": 500},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            confirmation_summary="ancien récap",
            interpreted_event="REJECT",
        )
        assert result["waiting_for_confirmation"] is False
        assert result["confirmation_summary"] is None
        assert result["expected_input"] == "NONE"
        assert result["status"] == "PLANNING"
        assert result["is_certified"] is False
        assert result["execution_authorized"] is False

    def test_reject_invites_a_correction_in_the_message(self):
        result = gate(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            transaction_payload={"product": "carottes", "price": 300, "quantity": 500},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            interpreted_event="REJECT",
        )
        assert "modifier" in result["final_response"]
        assert "annuler" in result["final_response"]


# =====================================================================
# CONFIRM — chemin inchangé pour TOUS les goals, y compris procurement
# =====================================================================

class TestConfirmPathIsUntouched:
    def test_procurement_confirm_still_authorizes_execution_normally(self):
        """Le fix ne touche QUE REJECT — CONFIRM (et donc l'écriture réelle
        via l'exécuteur générique, ex. create_auction) doit rester identique."""
        result = gate(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            transaction_payload={"product": "carottes", "price": 300, "quantity": 500},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            interpreted_event="CONFIRM",
        )
        assert result["is_certified"] is True
        assert result["execution_authorized"] is True
        assert result["status"] == "EXECUTING"

    def test_first_pass_raises_a_normal_confirmation_summary(self):
        result = gate(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            transaction_payload={"product": "carottes", "price": 300, "quantity": 500, "unit": "TONNE"},
        )
        assert result["waiting_for_confirmation"] is True
        assert result["response_strategy"] == "CONFIRMATION"
        assert "carottes" in result["confirmation_summary"]


# =====================================================================
# BOUT-EN-BOUT — routage du tour suivant après un REJECT préservé
# =====================================================================

class TestPostRejectRoutingRoundTrip:
    """Vérifie que l'état produit par le REJECT "doux" retombe bien sur
    `buyer_request_resolver` (via `to_resolver`) au tour suivant plutôt que
    d'être court-circuité vers une réponse générique — c'est précisément le
    routage qui manquait dans le bug production."""

    def test_router_sends_the_soft_rejected_state_back_to_the_resolver(self):
        from agriconnect.graphs.agents.market_coach.core.router import get_domain_router

        soft_rejected_state = gate(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            transaction_payload={"product": "carottes", "price": 300, "quantity": 500},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            interpreted_event="REJECT",
            working_memory={"active_goal": "PROCUREMENT_CREATE_REQUEST", "locked_intent": "PROCUREMENT_CREATE_REQUEST"},
        )
        # Simule le merge_dict du reducer : les champs non retournés par le
        # patch (current_goal, transaction_payload, working_memory) survivent
        # tels quels depuis l'état d'entrée.
        next_turn_state = {
            "current_goal": "PROCUREMENT_CREATE_REQUEST",
            "transaction_payload": {"product": "carottes", "price": 300, "quantity": 500},
            "working_memory": {"active_goal": "PROCUREMENT_CREATE_REQUEST", "locked_intent": "PROCUREMENT_CREATE_REQUEST"},
            "missing_fields": [],
            "interpreted_event": "UPDATE",  # l'utilisateur corrige le prix
            **soft_rejected_state,
        }
        router = get_domain_router()
        assert router.decide(next_turn_state) == "to_resolver"

    def test_buyer_request_resolver_re_escalates_with_a_corrected_price(self):
        """Le tour suivant (une correction de prix) doit ré-entrer dans le
        formulaire d'appel d'offres au lieu d'être traité comme un nouveau
        message sans contexte."""
        from agriconnect.graphs.agents.market_coach.flows.buyer.procurement import buyer_request_resolver
        from tests.conftest import StubRuntime

        state = make_state(
            user_phone="+2260",
            current_goal="PROCUREMENT_CREATE_REQUEST",
            active_form="AUCTION_CREATE",
            form_data={"product": "carottes", "unit": "KG"},
            transaction_payload={"product": "carottes", "price": 50_000_000, "quantity": 500},
            working_memory={"active_goal": "PROCUREMENT_CREATE_REQUEST", "locked_intent": "PROCUREMENT_CREATE_REQUEST"},
        )
        result = run(buyer_request_resolver(state, StubRuntime()))
        assert result["active_form"] == "AUCTION_CREATE"
        assert result["transaction_payload"]["product"] == "carottes"
