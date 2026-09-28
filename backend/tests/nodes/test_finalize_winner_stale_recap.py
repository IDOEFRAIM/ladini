"""`finalize_winner`/`_execute_winner_selection` — l'attribution s'exécute sur la DÉCISION CERTIFIÉE que
l'acheteur a confirmée, jamais sur une valeur périmée ni sur l'état mutable relu (2026-09-04 audit stale recap,
étendu Phase B2b : prix + BASE + quantité + total, pas seulement le montant).

## Le gap réel fermé par ce fichier

Entre le « oui » de l'acheteur (`confirm_winner_selection`) et l'exécution (`select_winning_bid`), une étape GPS
s'intercale — 1 ou plusieurs tours. Le producteur reste libre de modifier son prix / sa base ou de retirer son
offre PENDANT cette fenêtre. La revalidation compare l'EMPREINTE de la décision figée (`pending_award`) à l'état
réel : une divergence — y compris « 250 par tonne » devenu « 250 pour l'ensemble », que l'ancienne comparaison du
seul montant ne voyait pas — interrompt l'exécution et redemande une confirmation EXPLICITE."""
from __future__ import annotations

from typing import Any, Dict

from tests.conftest import StubRuntime, make_state, run
from tests.nodes.award_fixtures import bid_row, bids_response, frozen_state


def rt(responses=None):
    return StubRuntime(responses=responses or {})


def _gps_stage_state(amount: Any = 250, **wm_overrides) -> Dict[str, Any]:
    """État après « oui » ET étape GPS atteinte : la décision certifiée (`pending_award`) est ce qui a été montré
    et confirmé — c'est elle qui est comparée à l'état réel puis exécutée."""
    wm = {
        "pending_winner_bid": "b1",
        "winner_auction_id": "a1",
        "pending_award": frozen_state(amount),
        "pending_winner_price": float(amount),
        "winner_gps_stage": True,
        "winner_gps_default": {"lat": 12.35, "lon": -1.5},
    }
    wm.update(wm_overrides)
    return make_state(
        working_memory=wm,
        interpreted_event="CONFIRM",
        user_phone="+22670000001",
    )


class TestTermsUnchangedExecutesNormally:
    def test_same_terms_at_gps_resolution_executes_the_confirmed_decision(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import (
            finalize_winner,
        )

        sent: Dict[str, Any] = {}

        def _select(**kwargs):
            sent.update(kwargs)
            return {"status": "success", "summary_buyer": "🤝 C'est fait ! 250 FCFA"}

        state = _gps_stage_state()
        runtime = rt({"get_auction_bids": bids_response(bid_row(250)), "select_winning_bid": _select})
        result = run(finalize_winner(state, runtime))

        assert "select_winning_bid" in runtime.calls
        assert result["status"] == "COMPLETED"
        assert "250" in result["final_response"]
        # ce qui part au serveur : les termes CONFIRMÉS et la clé d'idempotence de la DÉCISION
        pending = state["working_memory"]["pending_award"]
        assert sent["expected_award"] == {"fingerprint": pending["fingerprint"]}
        assert sent["idempotency_key"] == pending["idempotency_key"]


class TestTermsChangedDuringGpsWindowAreCaughtNotSilentlyExecuted:
    def test_a_price_raised_between_confirm_and_gps_blocks_execution_and_reprompts(self):
        """bid=250/tonne affiché et approuvé, mise à jour producteur entre-temps (250 -> 300) : le système ne doit
        JAMAIS exécuter sur 250, ni exécuter silencieusement sur 300 sans reprévenir l'acheteur."""
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import (
            finalize_winner,
        )

        state = _gps_stage_state(250)
        runtime = rt({"get_auction_bids": bids_response(bid_row(300))})
        result = run(finalize_winner(state, runtime))

        assert "select_winning_bid" not in runtime.calls
        assert result["status"] == "WAITING_INPUT"
        assert "300 FCFA par tonne" in result["final_response"]
        assert "changé" in result["final_response"].lower()
        # La NOUVELLE décision devient la cible de confirmation.
        new = result["working_memory"]["pending_award"]
        assert new["fingerprint"] != state["working_memory"]["pending_award"]["fingerprint"]
        assert result["working_memory"]["pending_winner_bid"] == "b1"

    def test_a_basis_change_with_the_same_amount_is_caught_too(self):
        """« 250 par tonne » devenu « 250 pour l'ensemble » : même chiffre, TOUT autre sens — l'ancienne revalidation
        ne comparait que le montant."""
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import (
            finalize_winner,
        )

        state = _gps_stage_state(250)
        runtime = rt({"get_auction_bids": bids_response(bid_row(250, basis="TOTAL_LOT"))})
        result = run(finalize_winner(state, runtime))

        assert "select_winning_bid" not in runtime.calls
        assert result["status"] == "WAITING_INPUT"
        assert "pour l'ensemble" in result["final_response"]

    def test_re_confirming_after_a_change_goes_through_the_gps_stage_again(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import (
            finalize_winner,
        )

        state = _gps_stage_state(300, winner_gps_stage=None)
        state["normalized_text"] = "oui"
        state["interpreted_event"] = "CONFIRM"
        runtime = rt({"get_user_by_phone": {"status": "success", "data": {}}})

        result = run(finalize_winner(state, runtime))

        assert "select_winning_bid" not in runtime.calls
        assert result["status"] == "WAITING_INPUT"
        assert result["working_memory"]["winner_gps_stage"] is True


class TestRawStateMutationCannotChangeWhatIsExecuted:
    def test_mutating_the_raw_price_state_after_confirmation_still_executes_the_certified_award(self):
        """Étape 19 : l'acheteur confirme, un état brut (`pending_winner_price`, `transaction_payload.price`) est muté
        AVANT l'exécution -> l'exécution reste celle de la décision certifiée (empreinte revalidée côté serveur),
        jamais le prix mutable relu."""
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import (
            finalize_winner,
        )

        sent: Dict[str, Any] = {}

        def _select(**kwargs):
            sent.update(kwargs)
            return {"status": "success", "summary_buyer": "🤝 OK"}

        state = _gps_stage_state(250)
        certified = dict(state["working_memory"]["pending_award"])
        state["working_memory"]["pending_winner_price"] = 1.0  # muté
        state["transaction_payload"] = {"price": 1.0, "offered_price": 1.0, "bid_id": "b1"}  # muté
        runtime = rt({"get_auction_bids": bids_response(bid_row(250)), "select_winning_bid": _select})
        result = run(finalize_winner(state, runtime))

        assert result["final_response"] == "🤝 OK"
        assert sent["expected_award"] == {"fingerprint": certified["fingerprint"]}


class TestBidWithdrawnDuringGpsWindowIsRejectedCleanly:
    def test_a_bid_withdrawn_between_confirm_and_gps_never_executes(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import (
            finalize_winner,
        )

        state = _gps_stage_state()
        runtime = rt({"get_auction_bids": bids_response(bid_row(250, status="WITHDRAWN"))})
        result = run(finalize_winner(state, runtime))

        assert "select_winning_bid" not in runtime.calls
        assert result["status"] == "COMPLETED"
        assert result["response_strategy"] == "ERROR"
        assert "retirée" in result["final_response"].lower() or "disponible" in result["final_response"].lower()
        # État nettoyé — pas de reprompt sur une offre morte.
        assert result["working_memory"]["pending_winner_bid"] is None
        assert result["working_memory"]["pending_award"] is None


class TestLegacyBidBecomingUnqualifiedIsNeverAwarded:
    def test_a_bid_that_lost_its_basis_is_refused_with_a_requalification_message(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import (
            finalize_winner,
        )

        state = _gps_stage_state()
        runtime = rt({"get_auction_bids": bids_response(bid_row(250, legacy=True))})
        result = run(finalize_winner(state, runtime))

        assert "select_winning_bid" not in runtime.calls
        assert "n'indique pas" in result["final_response"]
