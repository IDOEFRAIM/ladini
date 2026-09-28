"""`flows/buyer/negotiation.py` — choisir une offre via la négociation ne crée PLUS de commande sur un numéro
de ligne (Phase B2b) : `_handle_viewing_offers` construit et affiche une décision d'attribution certifiée (prix +
BASE + quantité + total) ; `_handle_confirm_award` l'exécute après « oui » (même `select_winning_bid` que
`order_tracking.py::finalize_winner`, via `award_decision.execute_award`).

Cette machine à états n'a pas de slot multi-tour pour la confirmation GPS complète : elle réutilise en best-effort
le point GPS PAR DÉFAUT du profil s'il existe. Voir [[gps-delivery-burkina-faso-2026-08]]."""
from __future__ import annotations

from typing import Any, Dict

from tests.conftest import StubRuntime, make_state, run
from tests.nodes.award_fixtures import bid_row, bids_response


def _rt(select=None):
    responses = {"get_auction_bids": bids_response(bid_row(450000))}
    if select is not None:
        responses["select_winning_bid"] = select
    return StubRuntime(responses=responses)


def _pick(mod, runtime):
    return run(mod._handle_viewing_offers(
        mc_runtime=runtime,
        payload={"bid_id": "b1"},
        nctx={"buyer_phone": "+22670000001"},
        auction_id="a1",
        phone="+22670000001",
    ))


class TestPickingAnOfferOnlyPreparesTheConfirmation:
    def test_no_order_is_created_and_the_certified_terms_are_shown(self):
        import ladini.graphs.agents.market_coach.flows.buyer.negotiation as mod

        runtime = _rt(select={"status": "success"})
        result = _pick(mod, runtime)

        assert "select_winning_bid" not in runtime.calls
        assert result["status"] == "WAITING_INPUT"
        assert "450 000 FCFA par tonne" in result["final_response"]
        assert "4 500 000 FCFA" in result["final_response"]
        assert result["negotiation_context"]["phase"] == "CONFIRM_AWARD"
        assert result["negotiation_context"]["pending_award"]["fingerprint"]

    def test_a_bid_without_a_basis_is_never_offered_for_award(self):
        import ladini.graphs.agents.market_coach.flows.buyer.negotiation as mod

        runtime = StubRuntime(responses={"get_auction_bids": bids_response(bid_row(450000, legacy=True))})
        result = _pick(mod, runtime)

        assert "select_winning_bid" not in runtime.calls
        assert result["status"] == "COMPLETED"
        assert "n'indique pas" in result["final_response"]


class TestConfirmingExecutesTheCertifiedAward:
    def _confirm(self, mod, monkeypatch, stored_location):
        seen: Dict[str, Any] = {}

        def _select(**kwargs):
            seen.update(kwargs)
            return {"status": "success", "summary_buyer": "✅ Offre acceptée."}

        async def _fake_get_stored_location(mc_runtime, phone):
            return stored_location

        monkeypatch.setattr(
            "ladini.graphs.agents.market_coach.flows.buyer.order_tracking._get_stored_location",
            _fake_get_stored_location,
        )
        runtime = _rt(select=_select)
        picked = _pick(mod, runtime)
        state = make_state(
            interpreted_event="CONFIRM", normalized_text="oui", user_phone="+22670000001",
            negotiation_context=picked["negotiation_context"],
        )
        result = run(mod._handle_confirm_award(
            runtime, state, picked["negotiation_context"], "a1", "+22670000001",
        ))
        return result, seen, picked

    def test_accepting_a_bid_auto_attaches_the_buyers_default_location(self, monkeypatch):
        import ladini.graphs.agents.market_coach.flows.buyer.negotiation as mod

        result, seen, picked = self._confirm(mod, monkeypatch, (12.35, -1.5))

        assert result["response_strategy"] == "SUCCESS"
        assert seen["delivery_lat"] == 12.35
        assert seen["delivery_lon"] == -1.5
        frozen = picked["negotiation_context"]["pending_award"]
        assert seen["expected_award"] == {"fingerprint": frozen["fingerprint"]}
        assert seen["idempotency_key"] == frozen["idempotency_key"]

    def test_accepting_a_bid_without_any_stored_location_still_succeeds(self, monkeypatch):
        """Non-régression : pas de point GPS connu -> la commande se crée quand même (comportement historique),
        juste sans coordonnées."""
        import ladini.graphs.agents.market_coach.flows.buyer.negotiation as mod

        result, seen, _ = self._confirm(mod, monkeypatch, (None, None))

        assert result["response_strategy"] == "SUCCESS"
        assert seen["delivery_lat"] is None
        assert seen["delivery_lon"] is None

    def test_declining_creates_nothing(self):
        import ladini.graphs.agents.market_coach.flows.buyer.negotiation as mod

        runtime = _rt(select={"status": "success"})
        picked = _pick(mod, runtime)
        state = make_state(interpreted_event="REJECT", normalized_text="non", user_phone="+22670000001")
        result = run(mod._handle_confirm_award(runtime, state, picked["negotiation_context"], "a1", "+22670000001"))

        assert "select_winning_bid" not in runtime.calls
        assert "aucune proposition" in result["final_response"]

    def test_terms_changed_after_the_pick_require_a_new_confirmation(self):
        import ladini.graphs.agents.market_coach.flows.buyer.negotiation as mod

        runtime = _rt(select={"status": "success"})
        picked = _pick(mod, runtime)
        # le producteur passe à 430000 entre le choix et le « oui »
        runtime._responses["get_auction_bids"] = bids_response(bid_row(430000))
        state = make_state(interpreted_event="CONFIRM", normalized_text="oui", user_phone="+22670000001")
        result = run(mod._handle_confirm_award(runtime, state, picked["negotiation_context"], "a1", "+22670000001"))

        assert "select_winning_bid" not in runtime.calls
        assert result["status"] == "WAITING_INPUT"
        assert "430 000 FCFA par tonne" in result["final_response"]
