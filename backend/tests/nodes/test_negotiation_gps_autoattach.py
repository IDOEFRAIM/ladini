"""`flows/buyer/negotiation.py::_handle_viewing_offers` — accepter une offre
via la négociation crée aussi une commande (même `select_winning_bid` que
`order_tracking.py::finalize_winner`). Cette machine à états n'a pas de slot
multi-tour pour reproduire la confirmation GPS complète : elle réutilise en
best-effort le point GPS PAR DÉFAUT du profil s'il existe. Voir
[[gps-delivery-burkina-faso-2026-08]]."""
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock

from tests.conftest import run


class TestViewingOffersGpsAutoAttach:
    def test_accepting_a_bid_auto_attaches_the_buyers_default_location(self, monkeypatch):
        import ladini.graphs.agents.market_coach.flows.buyer.negotiation as mod

        seen: Dict[str, Any] = {}

        class _CapturingAuctionGateway:
            def __init__(self, rt):
                pass

            async def select_winning_bid(self, **kwargs):
                seen.update(kwargs)
                return {"status": "success", "summary_buyer": "✅ Offre acceptée."}

        async def _fake_get_stored_location(mc_runtime, phone):
            return 12.35, -1.5

        monkeypatch.setattr(mod, "AuctionGateway", _CapturingAuctionGateway)
        monkeypatch.setattr(
            "ladini.graphs.agents.market_coach.flows.buyer.order_tracking._get_stored_location",
            _fake_get_stored_location,
        )

        result = run(mod._handle_viewing_offers(
            mc_runtime=None,
            payload={"bid_id": "b1"},
            nctx={"buyer_phone": "+22670000001"},
            auction_id="a1",
        ))

        assert result["response_strategy"] == "SUCCESS"
        assert seen["delivery_lat"] == 12.35
        assert seen["delivery_lon"] == -1.5

    def test_accepting_a_bid_without_any_stored_location_still_succeeds(self, monkeypatch):
        """Non-régression : pas de point GPS connu -> la commande se crée
        quand même (comportement historique), juste sans coordonnées."""
        import ladini.graphs.agents.market_coach.flows.buyer.negotiation as mod

        seen: Dict[str, Any] = {}

        class _CapturingAuctionGateway:
            def __init__(self, rt):
                pass

            async def select_winning_bid(self, **kwargs):
                seen.update(kwargs)
                return {"status": "success", "summary_buyer": "✅ Offre acceptée."}

        async def _fake_get_stored_location(mc_runtime, phone):
            return None, None

        monkeypatch.setattr(mod, "AuctionGateway", _CapturingAuctionGateway)
        monkeypatch.setattr(
            "ladini.graphs.agents.market_coach.flows.buyer.order_tracking._get_stored_location",
            _fake_get_stored_location,
        )

        result = run(mod._handle_viewing_offers(
            mc_runtime=None,
            payload={"bid_id": "b1"},
            nctx={"buyer_phone": "+22670000001"},
            auction_id="a1",
        ))

        assert result["response_strategy"] == "SUCCESS"
        assert seen["delivery_lat"] is None
        assert seen["delivery_lon"] is None
