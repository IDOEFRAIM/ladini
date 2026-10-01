"""B1+B2 (2026-10-01) — replays de l'incident « poulets → je veux acheter du lait ».

Chaîne RÉELLE input_interpreter → FastPathPolicy → memory_update → validator →
cart_management (reducers LangGraph réels, voir test_tier_selection_full_node_chain).
Seul le LLM et les appels MCP sont doublés.

Deux couches :
- LAYER 1 (routing) : une nouvelle demande d'achat n'est jamais un ANSWER du slot.
- LAYER 2 (domaine) : sans quantité résolue, aucun effet de bord d'ajout au panier
  (stock, panier), quel que soit l'événement amont — reste vrai même si B1 régresse.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

import ladini.graphs.agents.market_coach.services.domain.cart_service as cart_service_mod
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
    set_pending_interaction,
    to_tunnel_category,
)
from ladini.graphs.agents.market_coach.core.policies import FastPathPolicy
from ladini.graphs.agents.market_coach.flows.buyer.cart import cart_management
from ladini.graphs.agents.market_coach.interpreter.routing import make_input_interpreter
from ladini.graphs.agents.market_coach.nodes.memory import memory_update
from ladini.graphs.agents.market_coach.nodes.validation import validator
from tests.conftest import StubRuntime, run
from tests.integration.test_tier_selection_full_node_chain import apply_patch
from tests.interpreter.test_active_slot_micro import _SequencedLLM

_PHONE = "+22601479800"


def _vendor(name: str, unit: str) -> Dict[str, Any]:
    return {
        "product_id": f"P-{name}",
        "name": name,
        "price": 2500.0,
        "unit": unit,
        "vendor_name": "jojo",
        "producer_id": "PR-JOJO",
        "source_type": "DIRECT",
        "is_auction": False,
        "available_quantity": 20,
    }


class _Rt(StubRuntime):
    """StubRuntime qui journalise (tool, kwargs) — nécessaire pour prouver l'absence
    d'appel de stock."""

    def __init__(self, llm: Any = None) -> None:
        super().__init__(
            llm=llm,
            responses={
                "validate_stock_availability_atomic": {
                    "status": "success",
                    "available_quantity": 20,
                }
            },
        )
        self.tool_calls: List[Tuple[str, Dict[str, Any]]] = []

    async def call_db(self, tool_name: str, **kwargs: Any) -> Any:
        self.tool_calls.append((tool_name, kwargs))
        return await super().call_db(tool_name, **kwargs)

    def stock_checks(self) -> List[Dict[str, Any]]:
        return [kw for n, kw in self.tool_calls if n == "validate_stock_availability_atomic"]


@pytest.fixture(autouse=True)
def _vendors(monkeypatch):
    async def _fake(self, phone, product_name):
        unit = "LITRE" if "lait" in str(product_name).lower() else "UNITE"
        return [_vendor(str(product_name), unit)], False

    monkeypatch.setattr(cart_service_mod.CartDomainService, "resolve_product_vendors", _fake)


def _awaiting_quantity_state(text: str, product: str = "poulets") -> Dict[str, Any]:
    return {
        "current_goal": "BUYER_ADD_TO_CART",
        "status": "WAITING_INPUT",
        "normalized_text": text,
        "user_query": text,
        "user_phone": _PHONE,
        "transaction_payload": {"product": product},
        "working_memory": {},
        "active_cart": [],
        **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"),
    }


def _after_turn1(rt: _Rt) -> Dict[str, Any]:
    """Tour 1 réel : « je veux acheter des poulets » (produit connu, quantité absente)
    — pose vendor_selection_context + pending ENTER_QUANTITY, panier vide."""
    state = _awaiting_quantity_state("je veux acheter des poulets")
    state = apply_patch(
        state,
        {
            "interpreted_event": "NEW_TASK",
            "detected_intent": "BUYER_ADD_TO_CART",
            "interpreter_confidence": 0.9,
            "extracted_entities": {"product": "poulets"},
        },
    )
    state = _run_buyer_nodes(state, rt)
    assert to_tunnel_category(get_pending_interaction(state)) == "QUANTITY"
    assert state["active_cart"] == []
    assert rt.stock_checks() == []
    return state


def _run_buyer_nodes(state: Dict[str, Any], rt: _Rt) -> Dict[str, Any]:
    state = apply_patch(state, run(memory_update(state, rt)))
    state = apply_patch(state, run(validator(state, rt)))
    return apply_patch(state, run(cart_management(state, rt)))


# ── LAYER 2 — défense en profondeur (indépendante de B1) ─────────────────────


class TestDomainSafetyNoQuantityNoSideEffect:
    @pytest.mark.parametrize("entities", [{}, {"product": "poulets"}])
    def test_answer_without_quantity_has_no_side_effect(self, entities):
        # État incohérent forcé : ANSWER + BUYER_ADD_TO_CART + produit + AUCUNE quantité.
        rt = _Rt()
        state = _awaiting_quantity_state("je ne sais pas")
        state = apply_patch(
            state,
            {
                "interpreted_event": "ANSWER",
                "detected_intent": "BUYER_ADD_TO_CART",
                "interpreter_confidence": 0.9,
                "extracted_entities": entities,
            },
        )
        cart_before = list(state["active_cart"])
        state = _run_buyer_nodes(state, rt)

        assert rt.stock_checks() == [], "stock validé sans quantité résolue"
        assert state["active_cart"] == cart_before, "panier muté sans quantité"
        assert not state["transaction_payload"].get("quantity"), state["transaction_payload"]
        assert "create_agent_action" not in [n for n, _ in rt.tool_calls]
        assert state["status"] != "COMPLETED"


# ── LAYER 1 — replay prod complet ────────────────────────────────────────────


class TestProdReplay:
    def test_new_purchase_during_enter_quantity_interrupts_cleanly(self):
        llm = _SequencedLLM(
            [
                # ACTIVE_SLOT se trompe (ANSWER + nouveau produit) …
                {
                    "disposition": "ANSWER",
                    "extracted_entities": {"product": "lait"},
                    "confidence": 0.95,
                },
                # … le classifieur NEW_TASK, lui, reconnaît la vraie intention.
                {
                    "disposition": "NEW_TASK",
                    "intent": "BUYER_REQUEST",
                    "confidence": 0.9,
                    "entities": {"product": "lait"},
                },
            ]
        )
        rt = _Rt(llm=llm)
        interp = make_input_interpreter("BUYER")
        state = _awaiting_quantity_state("je veux acheter du lait")
        state = apply_patch(state, run(interp(state, rt)))

        assert state["interpreted_event"] == "NEW_TASK"
        assert state["extracted_entities"].get("product") == "lait"
        # La fast-path acheteur est refusée → chaîne cognitive normale.
        assert FastPathPolicy.for_buyer().should_skip_cognitive(state) is False
        # Rien n'a été ajouté/validé pour les poulets.
        assert rt.stock_checks() == []
        assert state["active_cart"] == []

    def test_real_quantity_still_completes_the_cart_line(self):
        llm = _SequencedLLM(
            [
                {
                    "disposition": "ACTION",
                    "action": "SET_QUANTITY",
                    "quantity": 2,
                    "unit": None,
                    "confidence": 0.95,
                }
            ]
        )
        rt = _Rt(llm=llm)
        interp = make_input_interpreter("BUYER")
        state = _after_turn1(_Rt())
        state = apply_patch(state, {"normalized_text": "2", "user_query": "2"})
        state = apply_patch(state, run(interp(state, rt)))
        assert state["interpreted_event"] == "ANSWER", state.get("raw_analysis")
        assert FastPathPolicy.for_buyer().should_skip_cognitive(state) is True

        state = _run_buyer_nodes(state, rt)
        checks = rt.stock_checks()
        assert [c["quantity"] for c in checks] == [2.0]
        assert state["active_cart"][-1]["quantity"] == 2.0

    def test_hallucinated_quantity_one_on_a_new_purchase_is_rejected(self):
        # Voie RÉELLE de l'incident : après le tour 1 (vendeur choisi), la route est
        # STRUCTURED_ACTION/SET_QUANTITY. Le modèle invente quantity=1 pour « je veux
        # acheter du lait » (aucun nombre dans le message) → jamais un ANSWER.
        llm = _SequencedLLM(
            [
                {
                    "disposition": "ACTION",
                    "action": "SET_QUANTITY",
                    "quantity": 1,
                    "unit": "UNITE",
                    "confidence": 0.95,
                }
            ]
        )
        rt = _Rt(llm=llm)
        interp = make_input_interpreter("BUYER")
        state = _after_turn1(_Rt())
        state = apply_patch(
            state,
            {"normalized_text": "je veux acheter du lait", "user_query": "je veux acheter du lait"},
        )
        state = apply_patch(state, run(interp(state, rt)))
        assert state["interpreted_event"] == "UNKNOWN", state.get("raw_analysis")
        assert FastPathPolicy.for_buyer().should_skip_cognitive(state) is False
        assert not state["extracted_entities"].get("action_quantity")
        assert rt.stock_checks() == []
        assert state["active_cart"] == []

    def test_un_peu_de_lait_does_not_certify_quantity_one(self):
        llm = _SequencedLLM(
            [
                {
                    "disposition": "ACTION",
                    "action": "SET_QUANTITY",
                    "quantity": 1,
                    "unit": None,
                    "confidence": 0.95,
                }
            ]
        )
        rt = _Rt(llm=llm)
        interp = make_input_interpreter("BUYER")
        state = _after_turn1(_Rt())
        state = apply_patch(
            state, {"normalized_text": "un peu de lait", "user_query": "un peu de lait"}
        )
        state = apply_patch(state, run(interp(state, rt)))
        assert state["interpreted_event"] == "UNKNOWN", state.get("raw_analysis")
        assert rt.stock_checks() == []
        assert state["active_cart"] == []

    def test_new_purchase_deviation_on_structured_route_reclassifies(self):
        llm = _SequencedLLM(
            [
                {"disposition": "DEVIATION", "confidence": 0.9},
                {
                    "disposition": "NEW_TASK",
                    "intent": "BUYER_REQUEST",
                    "confidence": 0.9,
                    "entities": {"product": "lait"},
                },
            ]
        )
        rt = _Rt(llm=llm)
        interp = make_input_interpreter("BUYER")
        state = _after_turn1(_Rt())
        state = apply_patch(
            state,
            {"normalized_text": "je veux acheter du lait", "user_query": "je veux acheter du lait"},
        )
        state = apply_patch(state, run(interp(state, rt)))
        assert state["interpreted_event"] == "NEW_TASK"
        assert state["extracted_entities"].get("product") == "lait"
        assert rt.stock_checks() == []

    def test_dont_know_keeps_waiting_and_asks_again(self):
        llm = _SequencedLLM(
            [{"disposition": "ANSWER", "extracted_entities": {}, "confidence": 0.9}]
        )
        rt = _Rt(llm=llm)
        interp = make_input_interpreter("BUYER")
        state = _awaiting_quantity_state("je ne sais pas")
        state = apply_patch(state, run(interp(state, rt)))
        assert state["interpreted_event"] == "UNKNOWN"
        assert FastPathPolicy.for_buyer().should_skip_cognitive(state) is False
        assert rt.stock_checks() == []
        assert state["active_cart"] == []
        # Le pending de quantité n'est pas détruit par l'interpréteur.
        assert to_tunnel_category(get_pending_interaction(state)) == "QUANTITY"
