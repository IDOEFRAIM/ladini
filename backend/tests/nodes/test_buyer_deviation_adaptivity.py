"""Chantier résilience 2026-08 (volet acheteur) — mêmes gaps que côté
producteur : des points d'attente qui posent leur `final_response`
directement sur l'état court-circuitent le "reuse du final_response
précalculé" de `nodes/rendering/ask.py`/`menus.py`, empêchant ces renderers
génériques d'atteindre leur propre logique d'adaptivité LLM
(`utils.py::llm_deviation_reply`). Verrouille que ces points-là accusent
maintenant réception avant de rejouer leur texte figé :
  - `flows/buyer/cart.py::cart_management` — demande de quantité après
    résolution d'un vendeur unique.
  - `flows/buyer/negotiation.py::_handle_counter_price` — prix de
    contre-offre non reconnu.
  - `flows/buyer/negotiation.py::_handle_negotiation_menu` — action non
    résolue dans le menu de négociation.
"""
from __future__ import annotations

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)
from tests.conftest import StubRuntime, make_state, run


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Completion:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]


class _StubLLM:
    def __init__(self, text: str) -> None:
        self._text = text

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        return _Completion(self._text)


def rt_with_llm(text: str, responses=None):
    return StubRuntime(llm=_StubLLM(text), responses=responses or {})


# =====================================================================
# cart.py::cart_management — demande de quantité après vendeur résolu
# =====================================================================

class TestCartQuantityAskDeviation:
    def _vendor_ctx(self):
        return {
            "product": "tomates",
            "vendors": [{
                "product_id": "P-1", "name": "tomates", "price": 225, "unit": "KG",
                "vendor_name": "jojo", "producer_id": "PR-1",
                "source_type": "DIRECT", "is_auction": False,
            }],
            "chosen_vendor": {
                "product_id": "P-1", "name": "tomates", "price": 225, "unit": "KG",
                "vendor_name": "jojo", "producer_id": "PR-1",
                "source_type": "DIRECT", "is_auction": False,
            },
            "requested_quantity": None,
            "requested_unit": None,
            "available_mapping_kind": "product_vendor",
        }

    def test_unknown_event_deviation_gets_an_adaptive_note(self):
        from ladini.graphs.agents.market_coach.flows.buyer.cart import cart_management

        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            normalized_text="je sais pas trop, c'est pour combien de personnes en fait ?",
            interpreted_event="UNKNOWN",
            transaction_payload={"product": "tomates"},
            vendor_selection_context=self._vendor_ctx(),
        )
        runtime = rt_with_llm("Ça dépend surtout de vos besoins — pas de quantité fixe requise.")
        result = run(cart_management(state, runtime))
        assert result["final_response"].startswith("Ça dépend surtout")
        assert "quantité" in result["final_response"].lower()

    def test_answer_event_does_not_call_the_llm(self):
        """Sur l'entrée fraîche (produit tout juste résolu, event pas classé
        UNKNOWN/OUT_OF_SCOPE), pas d'appel LLM inutile."""
        from ladini.graphs.agents.market_coach.flows.buyer.cart import cart_management

        class _BoomLLM(_StubLLM):
            def create(self, **kwargs):
                raise AssertionError("le LLM ne devait pas être appelé ici")

        runtime = StubRuntime(responses={})
        runtime.llm = _BoomLLM("n/a")
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            normalized_text="je veux des tomates",
            interpreted_event="NEW_TASK",
            transaction_payload={"product": "tomates"},
            vendor_selection_context=self._vendor_ctx(),
        )
        result = run(cart_management(state, runtime))
        assert to_tunnel_category(get_pending_interaction(result)) == "QUANTITY"


# =====================================================================
# negotiation.py — prix de contre-offre / menu non résolus
# =====================================================================

class TestNegotiationCounterPriceDeviation:
    def test_unparsed_price_gets_an_adaptive_note(self):
        import ladini.graphs.agents.market_coach.flows.buyer.negotiation as mod

        state = make_state(normalized_text="je ne sais pas combien proposer honnêtement")
        runtime = rt_with_llm("Pas de souci, un montant approximatif suffit pour démarrer.")
        result = run(mod._handle_counter_price(
            mc_runtime=runtime, phone="+22670000001", payload={},
            nctx={}, auction_id="a1", state=state,
        ))
        assert result["final_response"].startswith("Pas de souci")
        assert "prix" in result["final_response"].lower()


class TestNegotiationMenuDeviation:
    def test_unresolved_action_gets_an_adaptive_note(self):
        import ladini.graphs.agents.market_coach.flows.buyer.negotiation as mod

        state = make_state(normalized_text="attendez c'est quoi la différence entre les options ?")
        runtime = rt_with_llm("Bonne question — chaque option agit différemment sur votre offre.")
        result = run(mod._handle_negotiation_menu(
            mc_runtime=runtime, phone="+22670000001", payload={},
            state=state, nctx={}, auction_id="a1",
        ))
        assert result["final_response"].startswith("Bonne question")
