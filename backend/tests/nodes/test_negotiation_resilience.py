"""`flows/buyer/negotiation.py` — chantier résilience 2026-08 : les 4 appels
MCP de ce fichier n'étaient protégés par aucun `try/except` local (contraste
avec `flows/producer/*.py`, qui entoure systématiquement ses appels gateway
équivalents). Le filet générique `_safe_node` (utils.py) évite bien un crash
dur du tour, mais ne nettoie PAS `negotiation_context` — un timeout MCP
laissait l'utilisateur bloqué dans une phase de négociation incohérente au
tour suivant. Verrouille : un échec gateway retourne un message clair ET
réinitialise `negotiation_context`, jamais de propagation d'exception."""
from __future__ import annotations

from tests.conftest import run


class _BoomGateway:
    def __init__(self, rt):
        pass


class TestFetchAndShowBidsResilience:
    def test_a_gateway_exception_is_caught_and_resets_the_negotiation_context(self, monkeypatch):
        import ladini.graphs.agents.market_coach.flows.buyer.negotiation as mod

        class _Boom(_BoomGateway):
            async def get_auction_bids(self, **kwargs):
                raise RuntimeError("mcp timeout")

        monkeypatch.setattr(mod, "AuctionGateway", _Boom)
        result = run(mod._fetch_and_show_bids(
            mc_runtime=None, auction_id="a1", nctx={"buyer_phone": "+22670000001"},
        ))
        assert result["response_strategy"] == "ERROR"
        assert result["negotiation_context"] == {"__reset__": True}
        assert "instant" in result["final_response"].lower()


class TestHandleCounterPriceResilience:
    def test_a_gateway_exception_is_caught_and_resets_the_negotiation_context(self, monkeypatch):
        """Phase B2c.6 : `update_offer` n'est plus appelé depuis `_handle_counter_price` (qui ne
        fait que CERTIFIER le prix et demander confirmation) mais depuis
        `_handle_confirm_negotiation_offer`, une fois le prix confirmé — voir `negotiation_offer.py`."""
        import ladini.graphs.agents.market_coach.flows.buyer.negotiation as mod

        nctx = {"buyer_phone": "+22670000001", "unit": "TONNE", "quantity": 10}
        state = {"normalized_text": "450000 la tonne", "user_query": "450000 la tonne"}
        confirm = run(mod._handle_counter_price(
            mc_runtime=None, phone="+22670000001", payload={}, nctx=nctx, auction_id="a1", state=state,
        ))
        assert confirm["negotiation_context"]["phase"] == "CONFIRM_NEGOTIATION_COUNTER"

        class _Boom(_BoomGateway):
            async def update_offer(self, **kwargs):
                raise RuntimeError("mcp timeout")

        # `claim_once` est une empreinte déterministe (mêmes termes -> même clé) : sans ce
        # monkeypatch, deux exécutions de CE test dans la même heure contre un Redis réel
        # partageraient la clé et le second run verrait "déjà en cours d'enregistrement" au lieu
        # de l'exception simulée — ce test vérifie le rattrapage d'erreur, pas l'idempotence.
        monkeypatch.setattr(mod, "claim_once", lambda *a, **k: True)
        monkeypatch.setattr(mod, "NegotiationGateway", _Boom)
        result = run(mod._handle_confirm_negotiation_offer(
            mc_runtime=None,
            state={"interpreted_event": "CONFIRM", "normalized_text": "oui"},
            nctx=confirm["negotiation_context"],
            phone="+22670000001",
            auction_id="a1",
        ))
        assert result["response_strategy"] == "ERROR"
        assert result["negotiation_context"] == {"__reset__": True}


class TestHandleViewingOffersResilience:
    def test_a_gateway_exception_is_caught_and_resets_the_negotiation_context(self, monkeypatch):
        import ladini.graphs.agents.market_coach.flows.buyer.negotiation as mod

        class _Boom(_BoomGateway):
            async def select_winning_bid(self, **kwargs):
                raise RuntimeError("mcp timeout")

        monkeypatch.setattr(mod, "AuctionGateway", _Boom)
        result = run(mod._handle_viewing_offers(
            mc_runtime=None, payload={"bid_id": "b1"},
            nctx={"buyer_phone": "+22670000001"}, auction_id="a1",
            phone="+22670000001",
        ))
        assert result["response_strategy"] == "ERROR"
        assert result["negotiation_context"] == {"__reset__": True}


class TestHandleNegotiationMenuResilience:
    def test_a_gateway_exception_on_close_still_ends_the_tunnel_cleanly(self, monkeypatch):
        """Même en échec technique côté serveur, l'utilisateur ne doit
        jamais rester bloqué sans porte de sortie — le tunnel se ferme côté
        état quoi qu'il arrive."""
        import ladini.graphs.agents.market_coach.flows.buyer.negotiation as mod

        class _Boom(_BoomGateway):
            async def close_session(self, **kwargs):
                raise RuntimeError("mcp timeout")

        monkeypatch.setattr(mod, "NegotiationGateway", _Boom)
        result = run(mod._handle_negotiation_menu(
            mc_runtime=None, phone="+22670000001",
            payload={"resolved_id": "NEGOTIATION_ABORT"},
            state={}, nctx={"buyer_phone": "+22670000001"}, auction_id="a1",
        ))
        assert result["status"] == "COMPLETED"
        assert result["current_goal"] is None
        assert result["negotiation_context"] == {"__reset__": True}
        assert "annulée" in result["final_response"].lower()
