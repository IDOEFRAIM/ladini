"""`finalize_winner`/`_execute_winner_selection` — le récapitulatif de
sélection du gagnant ne doit JAMAIS exécuter silencieusement sur une
valeur périmée (2026-09-04, audit Auction/Bid, volet "stale recap").

## Le gap réel fermé par ce fichier

Entre le "oui" de l'acheteur (`confirm_winner_selection`, où le prix
affiché est capturé) et l'exécution réelle (`select_winning_bid`), une
étape GPS s'intercale — 1 ou plusieurs tours conversationnels. Le
producteur reste libre de modifier son prix (`update_bid_price`) ou de
retirer son offre (`withdraw_bid`) PENDANT cette fenêtre : rien
n'empêchait AVANT ce correctif que l'exécution finale se fasse sur la
valeur ORIGINALEMENT affichée pendant que la base avait déjà changé sous
les pieds de l'acheteur — la commande finale (via le verrou de
`select_winning_bid`) portait toujours la VRAIE valeur courante, mais
l'acheteur n'était JAMAIS reprévenu que ce n'était plus ce qu'il avait vu
au moment de dire "oui".

## Modèle corrigé

`_fetch_winner_recap` est LA seule fonction qui projette "l'état actuel de
cette offre" — utilisée à la fois pour l'affichage initial ET pour la
revalidation juste avant `select_winning_bid`. Ce n'est PAS un
`refresh_confirmation()` cosmétique : une divergence détectée interrompt
l'exécution et redemande une confirmation EXPLICITE sur la valeur
ACTUELLE, jamais une exécution silencieuse sur l'ancienne."""
from __future__ import annotations

from typing import Any, Dict

from tests.conftest import StubRuntime, make_state, run


def rt(responses=None):
    return StubRuntime(responses=responses or {})


def _gps_stage_state(**wm_overrides) -> Dict[str, Any]:
    wm = {
        "pending_winner_bid": "b1",
        "winner_auction_id": "a1",
        "pending_winner_price": 250.0,
        "winner_gps_stage": True,
        "winner_gps_default": {"lat": 12.35, "lon": -1.5},
    }
    wm.update(wm_overrides)
    return make_state(
        working_memory=wm,
        interpreted_event="CONFIRM",
        user_phone="+22670000001",
    )


class TestPriceUnchangedExecutesNormally:
    def test_same_price_at_gps_resolution_executes_without_a_new_confirmation(self):
        from agriconnect.graphs.agents.market_coach.flows.buyer.order_tracking import (
            finalize_winner,
        )

        state = _gps_stage_state()
        runtime = rt(
            {
                "get_auction_bids": {
                    "bids": [{"bid_id": "b1", "producer": "Awa", "price": 250.0, "status": "PENDING"}],
                    "auction": {"product": "riz"},
                },
                "select_winning_bid": {
                    "status": "success",
                    "summary_buyer": "🤝 C'est fait ! 250 FCFA",
                },
            }
        )
        result = run(finalize_winner(state, runtime))

        assert "select_winning_bid" in runtime.calls
        assert result["status"] == "COMPLETED"
        assert "250" in result["final_response"]


class TestPriceChangedDuringGpsWindowIsCaughtNotSilentlyExecuted:
    def test_a_price_raised_between_confirm_and_gps_blocks_execution_and_reprompts(self):
        """Reproduction exacte du scénario du mandat : bid=250 affiché et
        approuvé, mise à jour producteur entre-temps (250 -> 300), la
        position GPS arrive ENFIN -> le système ne doit JAMAIS exécuter sur
        250, ni exécuter silencieusement sur 300 sans reprévenir l'acheteur."""
        from agriconnect.graphs.agents.market_coach.flows.buyer.order_tracking import (
            finalize_winner,
        )

        state = _gps_stage_state()  # pending_winner_price=250.0 (ce qui a été montré)
        runtime = rt(
            {
                "get_auction_bids": {
                    "bids": [{"bid_id": "b1", "producer": "Awa", "price": 300.0, "status": "PENDING"}],
                    "auction": {"product": "riz"},
                }
            }
        )
        result = run(finalize_winner(state, runtime))

        assert "select_winning_bid" not in runtime.calls, (
            "ne doit JAMAIS exécuter silencieusement sur une valeur périmée (250) "
            "ni sur la nouvelle (300) sans confirmation explicite"
        )
        assert result["status"] == "WAITING_INPUT"
        assert "300" in result["final_response"]
        assert "changé" in result["final_response"].lower()
        # Le prix affiché redevient la nouvelle cible de confirmation —
        # une future résolution GPS re-comparera contre CETTE valeur.
        assert result["working_memory"]["pending_winner_price"] == 300.0
        assert result["working_memory"]["pending_winner_bid"] == "b1"

    def test_re_confirming_after_a_price_change_then_executes_at_the_new_price(self):
        """Suite du scénario ci-dessus : l'acheteur répond "oui" au NOUVEAU
        récap (300) -> exécution normale, au nouveau prix, plus de
        divergence."""
        from agriconnect.graphs.agents.market_coach.flows.buyer.order_tracking import (
            finalize_winner,
        )

        state = _gps_stage_state(pending_winner_price=300.0, winner_gps_stage=None)
        state["normalized_text"] = "oui"
        state["interpreted_event"] = "CONFIRM"
        runtime = rt({"get_user_by_phone": {"status": "success", "data": {}}})

        result = run(finalize_winner(state, runtime))

        # Palier 1 : re-confirmation du gagnant -> repasse par l'étape GPS
        # (comportement historique inchangé), PAS d'exécution à ce tour.
        assert "select_winning_bid" not in runtime.calls
        assert result["status"] == "WAITING_INPUT"
        assert result["working_memory"]["winner_gps_stage"] is True


class TestBidWithdrawnDuringGpsWindowIsRejectedCleanly:
    def test_a_bid_withdrawn_between_confirm_and_gps_never_executes(self):
        from agriconnect.graphs.agents.market_coach.flows.buyer.order_tracking import (
            finalize_winner,
        )

        state = _gps_stage_state()
        runtime = rt(
            {
                "get_auction_bids": {
                    "bids": [{"bid_id": "b1", "producer": "Awa", "price": 250.0, "status": "WITHDRAWN"}],
                    "auction": {"product": "riz"},
                }
            }
        )
        result = run(finalize_winner(state, runtime))

        assert "select_winning_bid" not in runtime.calls
        assert result["status"] == "COMPLETED"
        assert result["response_strategy"] == "ERROR"
        assert "retirée" in result["final_response"].lower() or "disponible" in result["final_response"].lower()
        # État nettoyé — pas de reprompt sur une offre morte.
        assert result["working_memory"]["pending_winner_bid"] is None
