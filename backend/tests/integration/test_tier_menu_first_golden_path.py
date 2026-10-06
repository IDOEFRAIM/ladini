"""Golden-path validation — "Menu-First" sequence for `pricing_tiers`
(2026-09-01, explicit Lead Architect directive).

Runs the REAL node chain (input_interpreter → FastPathPolicy bypass →
memory_update → validator → cart_management → turn-boundary cleanup) for the
exact 4-step flow specified:

  1. "Je veux du lait" (no quantity mentioned at all) → the system must show
     the tier/packaging menu IMMEDIATELY, never ask for a raw quantity first.
  2. "2" (bare digit) → deterministic, LLM-free resolution to the tier_id of
     the SECOND tier — never UNKNOWN, never a re-shown menu.
  3. System asks "combien de bidons de 10L ?" — the pack-count question is
     scoped to the CHOSEN packaging, not a generic quantity ask.
  4. "3" → 3 packs x 900 FCFA = 2700 FCFA, added to cart.

Uses the same `apply_patch` (real LangGraph reducer per field) helper as
`test_tier_selection_full_node_chain.py`.
"""
from __future__ import annotations

from typing import Any, Dict

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)
from ladini.graphs.agents.market_coach.flows.buyer.cart import cart_management
from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from ladini.graphs.agents.market_coach.nodes.cleaner import state_cleaner_node
from ladini.graphs.agents.market_coach.nodes.cleanup import post_response_cleanup
from ladini.graphs.agents.market_coach.nodes.memory import memory_update
from ladini.graphs.agents.market_coach.nodes.validation import validator
from tests.conftest import StubRuntime, run
from tests.integration.test_tier_selection_full_node_chain import apply_patch


def _tiered_vendor() -> Dict[str, Any]:
    return {
        "product_id": "P-LAIT",
        "name": "lait",
        "price": 500.0,
        "unit": "LITRE",
        "vendor_name": "jojo",
        "producer_id": "PR-JOJO",
        "source_type": "DIRECT",
        "is_auction": False,
        "pricing_tiers": [
            {"tier_id": "t5", "quantity": 5.0, "unit": "L", "price": 500.0,
             "packaging": "bidon", "base_unit_quantity": 5.0, "min_order_quantity": 1},
            {"tier_id": "t10", "quantity": 10.0, "unit": "L", "price": 900.0,
             "packaging": "bidon", "base_unit_quantity": 10.0, "min_order_quantity": 1},
        ],
    }


def _turn_boundary(state: Dict[str, Any], runtime: StubRuntime) -> Dict[str, Any]:
    state = apply_patch(state, run(state_cleaner_node(state, runtime)))
    state = apply_patch(state, run(post_response_cleanup(state, runtime)))
    return state


class TestMenuFirstGoldenPath:
    def test_intent_then_format_choice_then_pack_count_then_cart(self, monkeypatch):
        import ladini.graphs.agents.market_coach.services.domain.cart_service as cart_service_mod

        async def _fake_resolve_vendors(self, phone, product_name):
            return [_tiered_vendor()], False

        monkeypatch.setattr(
            cart_service_mod.CartDomainService,
            "resolve_product_vendors",
            _fake_resolve_vendors,
        )

        interpreter = make_input_interpreter("BUYER")
        runtime = StubRuntime()

        # ---------------- STEP 1: "Je veux du lait" — NO quantity ---------
        state: Dict[str, Any] = {
            "current_goal": "BUYER_ADD_TO_CART",
            "expected_input": "NONE",
            "status": "PROCESSING",
            "normalized_text": "je veux du lait",
            "user_query": "je veux du lait",
            "user_phone": "+22601479800",
            "transaction_payload": {"product": "lait"},
            "working_memory": {},
            "active_cart": [],
        }
        cart1 = run(cart_management(state, runtime))
        state = apply_patch(state, cart1)

        assert state["status"] == "WAITING_INPUT"
        assert to_tunnel_category(get_pending_interaction(state)) == "SELECTION", (
            "the tier menu must be the FIRST question — never a raw quantity ask"
        )
        assert "quantité" not in (state.get("final_response") or "").lower(), (
            "quantity must never be asked before the packaging is chosen"
        )
        assert "conditionnements" in (state.get("final_response") or "")
        tier_ctx = state.get("tier_selection_context")
        assert tier_ctx and tier_ctx.get("tiers"), "tier menu never seeded"
        state = _turn_boundary(state, runtime)

        # ---------------- STEP 2: "2" — deterministic digit -> tier_id ----
        state["normalized_text"] = "2"
        state["user_query"] = "2"
        interp2 = run(interpreter(state, runtime))
        state = apply_patch(state, interp2)
        assert state["interpreted_event"] == "SELECTION", state.get("raw_analysis")
        assert state["detected_intent"] != "UNKNOWN" or state["interpreted_event"] == "SELECTION", (
            "a bare digit under expected_input=SELECTION must resolve "
            "deterministically, never fall to UNKNOWN routing"
        )
        # (2026-09-01, contrat d'action structurée) : remplace l'ancien
        # `selection_index` générique — voir domain/selection_actions.py.
        assert state["extracted_entities"].get("agent_action") == "SELECT_PRICING_TIER"
        assert state["extracted_entities"].get("action_pricing_tier_id") == "t10"
        assert state["raw_analysis"]["path"] == "fast_path_selection_action", (
            "must be resolved by the deterministic fast-path, never the LLM"
        )

        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        state = apply_patch(state, run(cart_management(state, runtime)))

        # ---------------- STEP 3: pack-count question, scoped to the tier -
        assert state["status"] == "WAITING_INPUT"
        assert to_tunnel_category(get_pending_interaction(state)) == "QUANTITY"
        assert "10.0 L" in (state.get("final_response") or ""), state.get("final_response")
        assert "bidon" in (state.get("final_response") or "").lower()
        assert state.get("vendor_selection_context", {}).get("resolved_tier_id") == "t10"
        # (2026-09-01) Le contexte n'est plus DÉTRUIT à la résolution : il est
        # ESTAMPILLÉ `resolved_tier_id`. Le menu ne se réaffiche plus (assertion
        # `expected_input == "QUANTITY"` + question ciblée ci-dessus), mais la
        # liste reste résoluble pour un changement d'avis explicite ("finalement
        # le bidon de 5 L") — impossible tant que le contexte était effacé.
        _tier_ctx = state.get("tier_selection_context")
        assert _tier_ctx and _tier_ctx.get("resolved_tier_id") == "t10", (
            "the tier menu context must be stamped as resolved, not destroyed"
        )
        assert (state.get("working_memory") or {}).get("available_mapping_kind") is None, (
            "a closed tier menu must stop claiming the 'pricing_tier' mapping kind"
        )
        state = _turn_boundary(state, runtime)

        # ---------------- STEP 4: "3" packs -> 3 x 900 = 2700 FCFA --------
        state["normalized_text"] = "3"
        state["user_query"] = "3"
        interp3 = run(interpreter(state, runtime))
        state = apply_patch(state, interp3)
        assert state["interpreted_event"] == "ANSWER", state.get("raw_analysis")

        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        state = apply_patch(state, run(cart_management(state, runtime)))

        assert state["status"] == "COMPLETED", state.get("final_response")
        line = state["active_cart"][-1]
        assert line["tier_id"] == "t10"
        assert line["quantity"] == 3
        assert line["base_unit_quantity"] == 30.0  # 3 x 10L
        assert line["line_total"] == 2700.0  # 3 x 900 FCFA
