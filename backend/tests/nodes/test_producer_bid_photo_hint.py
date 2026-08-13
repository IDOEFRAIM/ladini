"""`flows/producer/auctions.py::submit_bid` — le tunnel de confirmation
producteur construit son propre `final_response` (bypass du rendu générique
nodes/rendering/success.py, voir sa docstring "les nœuds negotiation posent
final_response directement") — vérifié en usage réel que le hint photo
n'apparaissait PAS après une offre placée via ce chemin. Distinct de
tests/nodes/test_rendering_success.py::TestAuctionAndBidPhotoHooks, qui
couvre le chemin générique (ex: create_auction, jamais bypassé côté acheteur).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

from tests.conftest import make_state, run


class TestSubmitBidPhotoHint:
    def test_a_successful_bid_sets_a_pending_photo_target_and_adds_a_hint(self, monkeypatch):
        from agriconnect.graphs.agents.market_coach.flows.producer import auctions as auctions_mod
        from agriconnect.graphs.agents.market_coach.services.mcp.gateway import AuctionGateway

        monkeypatch.setattr(
            AuctionGateway, "place_bid",
            AsyncMock(return_value={"status": "success", "bid_id": "b1", "message": "✅ Offre transmise."}),
        )
        captured = {}
        monkeypatch.setattr(
            auctions_mod, "set_pending_bid_photo",
            lambda phone, bid_id: captured.update(phone=phone, bid_id=bid_id),
        )

        state = make_state(user_phone="+22670000001")
        result = run(auctions_mod.submit_bid(state, mc_runtime=None, auction_id="a1", price=550.0))

        assert captured == {"phone": "+22670000001", "bid_id": "b1"}
        assert "📸" in result["final_response"]
        assert "photo" in result["final_response"].lower()
        assert "✅ Offre transmise." in result["final_response"]

    def test_no_photo_hint_when_the_db_result_has_no_bid_id(self, monkeypatch):
        """Repli défensif : un `place_bid` réussi mais sans `bid_id` (forme
        inattendue) ne doit jamais planter — juste pas de hint/marqueur."""
        from agriconnect.graphs.agents.market_coach.flows.producer import auctions as auctions_mod
        from agriconnect.graphs.agents.market_coach.services.mcp.gateway import AuctionGateway

        monkeypatch.setattr(
            AuctionGateway, "place_bid",
            AsyncMock(return_value={"status": "success", "message": "✅ Offre transmise."}),
        )
        called = {"count": 0}
        monkeypatch.setattr(
            auctions_mod, "set_pending_bid_photo",
            lambda phone, bid_id: called.__setitem__("count", called["count"] + 1),
        )

        state = make_state(user_phone="+22670000001")
        result = run(auctions_mod.submit_bid(state, mc_runtime=None, auction_id="a1", price=550.0))

        assert called["count"] == 0
        assert "📸" not in result["final_response"]

    def test_a_failed_bid_never_sets_a_pending_photo_target(self, monkeypatch):
        from agriconnect.graphs.agents.market_coach.flows.producer import auctions as auctions_mod
        from agriconnect.graphs.agents.market_coach.services.mcp.gateway import AuctionGateway

        monkeypatch.setattr(
            AuctionGateway, "place_bid",
            AsyncMock(return_value={"status": "error", "message": "Enchère fermée."}),
        )
        called = {"count": 0}
        monkeypatch.setattr(
            auctions_mod, "set_pending_bid_photo",
            lambda phone, bid_id: called.__setitem__("count", called["count"] + 1),
        )

        state = make_state(user_phone="+22670000001")
        result = run(auctions_mod.submit_bid(state, mc_runtime=None, auction_id="a1", price=550.0))

        assert called["count"] == 0
        assert result["response_strategy"] == "ERROR"
