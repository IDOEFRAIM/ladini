"""Repro for the STILL-live tier-menu loop (2026-08-30, latest report):
"ca marche toujours pas" — the tier menu reappears on a SELECTION reply via
a code path where `cart_management` completes in ~7ms with NO
`search_products` call.

Everything in `test_tier_selection_full_node_chain.py` gives the buyer's
FIRST reply-to-a-menu turn quantity+product together, so `cart_management`
always takes the "single vendor, quantity known → tier menu" branch
(cart.py:611-670) and its second turn re-resolves vendors via a FRESH
search (no `vendor_selection_context` was ever seeded). That is NOT what
happened live: the vendor was asked-and-resolved on an EARLIER turn (the
"ask quantity" branch at cart.py:569-610, which seeds
`vendor_selection_context` with `chosen_vendor` already set and NO
quantity yet). Only THEN does the buyer give the quantity, which routes
through the completely different `vendor_ctx_active` branch
(cart.py:201-440) — that's the "no search_products call" signature from
the log. This test reproduces that exact 3-turn sequence through the real
node chain to see whether `vendor_ctx_active`'s tier resolution actually
works once reached this way, instead of continuing to reason about it by
manual code reading alone.
"""
from __future__ import annotations

from typing import Any, Dict

from agriconnect.graphs.agents.market_coach.flows.buyer.cart import cart_management
from agriconnect.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from agriconnect.graphs.agents.market_coach.nodes.cleaner import state_cleaner_node
from agriconnect.graphs.agents.market_coach.nodes.cleanup import post_response_cleanup
from agriconnect.graphs.agents.market_coach.nodes.memory import memory_update
from agriconnect.graphs.agents.market_coach.nodes.validation import validator
from tests.conftest import StubRuntime, run
from tests.integration.test_tier_selection_full_node_chain import apply_patch
from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)


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


def _other_vendor() -> Dict[str, Any]:
    return {
        "product_id": "P-LAIT-2",
        "name": "lait",
        "price": 480.0,
        "unit": "LITRE",
        "vendor_name": "mariam",
        "producer_id": "PR-MARIAM",
        "source_type": "DIRECT",
        "is_auction": False,
        "pricing_tiers": [],
    }


def _run_turn_boundary(state: Dict[str, Any], runtime: StubRuntime) -> Dict[str, Any]:
    state = apply_patch(state, run(state_cleaner_node(state, runtime)))
    state = apply_patch(state, run(post_response_cleanup(state, runtime)))
    return state


class TestVendorCtxActiveTierChain:
    def test_three_turns_ask_quantity_then_tier_menu_then_selection(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.services.domain.cart_service as cart_service_mod

        search_calls = []

        async def _fake_resolve_vendors(self, phone, product_name):
            search_calls.append(product_name)
            return [_tiered_vendor(), _other_vendor()], True

        monkeypatch.setattr(
            cart_service_mod.CartDomainService,
            "resolve_product_vendors",
            _fake_resolve_vendors,
        )

        interpreter = make_input_interpreter("BUYER")
        runtime = StubRuntime()

        # ---------------- TURN 1: "60 litres de lait" (product+quantity) ---
        # The "missing product or quantity" guard (cart.py:443) runs BEFORE
        # any vendor search — a search only ever happens once BOTH are known.
        # → 2 vendors found → menu shown, vendor_selection_context seeded
        # WITHOUT chosen_vendor yet, but WITH requested_quantity=60
        # (cart_service.build_product_selection_menu extra_context).
        state: Dict[str, Any] = {
            "current_goal": "BUYER_ADD_TO_CART",
            "expected_input": "NONE",
            "status": "PROCESSING",
            "normalized_text": "60 litres de lait",
            "user_query": "60 litres de lait",
            "user_phone": "+22601479800",
            "transaction_payload": {"product": "lait", "quantity": 60, "unit": "LITRE"},
            "working_memory": {},
            "active_cart": [],
        }
        state = apply_patch(state, run(cart_management(state, runtime)))
        assert to_tunnel_category(get_pending_interaction(state)) == "SELECTION"
        vctx = state.get("vendor_selection_context")
        assert isinstance(vctx, dict) and vctx.get("vendors"), (
            f"turn 1 did not seed vendor_selection_context with a vendor list: {vctx}"
        )
        assert not vctx.get("chosen_vendor")
        assert vctx.get("requested_quantity") == 60
        assert len(search_calls) == 1

        state = _run_turn_boundary(state, runtime)
        assert state.get("vendor_selection_context", {}).get("vendors"), (
            "vendor_selection_context lost across turn-boundary cleanup nodes"
        )

        # ---------------- TURN 2: "1" (picks the first vendor) -------------
        # Enters vendor_ctx_active (cart.py:201+), resolves chosen_vendor via
        # selection_index, RECOVERS quantity=60 from vendor_ctx.requested_
        # quantity (cart.py:269-273) — so quantity is ALREADY known on this
        # very turn. The chosen vendor has pricing_tiers, so this shows the
        # TIER MENU directly (cart.py:305-378), with NO further vendor
        # search — this exactly matches the live log's "no search_products
        # call, 6.8ms" signature.
        state["normalized_text"] = "1"
        state["user_query"] = "1"

        interp0 = run(interpreter(state, runtime))
        state = apply_patch(state, interp0)
        assert state["interpreted_event"] == "SELECTION", state.get("raw_analysis")

        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        search_calls.clear()
        state = apply_patch(state, run(cart_management(state, runtime)))
        assert not search_calls, f"turn 2 re-searched vendors unexpectedly: {search_calls}"

        assert state["status"] == "WAITING_INPUT"
        assert to_tunnel_category(get_pending_interaction(state)) == "SELECTION"
        assert "conditionnements" in (state.get("final_response") or ""), state.get("final_response")
        tier_ctx = state.get("tier_selection_context")
        assert tier_ctx and tier_ctx.get("tiers"), "tier menu never seeded tier_selection_context"

        state = _run_turn_boundary(state, runtime)
        assert state.get("tier_selection_context", {}).get("tiers"), (
            "tier_selection_context lost across turn-boundary cleanup nodes"
        )

        # ---------------- TURN 3: "2" (reply to the tier menu) -------------
        # THE untested combination: vendor_selection_context.chosen_vendor
        # was resolved LOCALLY on turn 2 (from selection_index) but the
        # tier-menu response patch (cart.py:357-378) never persists the
        # updated vendor_ctx_payload back into state — only the "ask
        # quantity" branch (cart.py:437) does that. So on THIS turn,
        # state["vendor_selection_context"]["chosen_vendor"] may still be
        # the STALE None from turn 1's menu-seeding, not turn 2's resolution.
        state["normalized_text"] = "2"
        state["user_query"] = "2"

        interp1 = run(interpreter(state, runtime))
        state = apply_patch(state, interp1)
        assert state["interpreted_event"] == "SELECTION", state.get("raw_analysis")

        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))

        search_calls.clear()
        state = apply_patch(state, run(cart_management(state, runtime)))
        assert not search_calls, f"turn 3 re-searched vendors unexpectedly: {search_calls}"

        # (2026-09-01, Option 1 — "aucune conversion silencieuse quantité
        # globale -> nombre de paquets") : `quantity=60` a été donné au
        # TOUR 1, AVANT même que le menu de paliers existe — ce n'est pas
        # une réponse à "combien de bidons de 10L ?". Turn 3 doit ignorer
        # ce 60 hérité et reposer explicitement la question du nombre de
        # paquets, jamais compléter avec 60 bidons (600L) silencieusement.
        assert state["status"] == "WAITING_INPUT", (
            f"turn 3 must ask for the pack count, not silently reuse the "
            f"pre-tier flat quantity — status={state['status']} "
            f"final_response={state.get('final_response')!r}"
        )
        assert to_tunnel_category(get_pending_interaction(state)) == "QUANTITY"
        assert "10.0 L" in (state.get("final_response") or "")
        assert state.get("vendor_selection_context", {}).get("resolved_tier_id") == "t10"

        state = _run_turn_boundary(state, runtime)

        # ---------------- TURN 4: "5" (réponse au nombre de paquets) ------
        state["normalized_text"] = "5"
        state["user_query"] = "5"
        interp4 = run(interpreter(state, runtime))
        state = apply_patch(state, interp4)
        assert state["interpreted_event"] == "ANSWER", state.get("raw_analysis")

        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))

        search_calls.clear()
        state = apply_patch(state, run(cart_management(state, runtime)))
        assert not search_calls, f"turn 4 re-searched vendors unexpectedly: {search_calls}"

        assert state["status"] == "COMPLETED", (
            f"turn 4 did not complete the cart insertion — status={state['status']} "
            f"final_response={state.get('final_response')!r}"
        )
        line = state["active_cart"][-1]
        assert line["tier_id"] == "t10"
        assert line["base_unit_quantity"] == 50.0  # 5 packs x 10L

    def test_three_turns_free_text_reply_to_tier_menu(self, monkeypatch):
        """Same vendor_ctx_active setup as the first test, but the reply to
        the tier menu is free text instead of a bare digit — the OTHER
        live-reported failure mode. (2026-08-30, refonte "LLM pilote la
        sélection de palier") — free-text tier resolution is now the LLM's
        job (scripted here), informed by the tier list injected into its
        prompt; the earlier turns (bare digits) stay LLM-free since they
        never needed it."""
        import agriconnect.graphs.agents.market_coach.services.domain.cart_service as cart_service_mod
        from tests.conftest import ScriptedLLM

        async def _fake_resolve_vendors(self, phone, product_name):
            return [_tiered_vendor(), _other_vendor()], True

        monkeypatch.setattr(
            cart_service_mod.CartDomainService,
            "resolve_product_vendors",
            _fake_resolve_vendors,
        )

        interpreter = make_input_interpreter("BUYER")
        runtime = StubRuntime()

        state: Dict[str, Any] = {
            "current_goal": "BUYER_ADD_TO_CART",
            "expected_input": "NONE",
            "status": "PROCESSING",
            "normalized_text": "60 litres de lait",
            "user_query": "60 litres de lait",
            "user_phone": "+22601479800",
            "transaction_payload": {"product": "lait", "quantity": 60, "unit": "LITRE"},
            "working_memory": {},
            "active_cart": [],
        }
        state = apply_patch(state, run(cart_management(state, runtime)))
        assert to_tunnel_category(get_pending_interaction(state)) == "SELECTION"
        assert state.get("vendor_selection_context", {}).get("requested_quantity") == 60
        state = _run_turn_boundary(state, runtime)

        # Pick vendor #1 — quantity auto-recovers from requested_quantity,
        # so this same turn goes straight to the tier menu (no re-ask).
        state["normalized_text"] = "1"
        state["user_query"] = "1"
        state = apply_patch(state, run(interpreter(state, runtime)))
        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        state = apply_patch(state, run(cart_management(state, runtime)))
        assert to_tunnel_category(get_pending_interaction(state)) == "SELECTION"
        assert state.get("tier_selection_context", {}).get("tiers")
        state = _run_turn_boundary(state, runtime)

        state["normalized_text"] = "je veux celui de 10 L"
        state["user_query"] = "je veux celui de 10 L"

        scripted_llm = ScriptedLLM(
            {
                "interpreted_event": "SELECTION",
                "detected_intent": "UNKNOWN",
                "interpreter_confidence": 1.0,
                "validation_status": "VALID",
                "extracted_entities": {"selected_value": "t10"},
            }
        )
        runtime = StubRuntime(llm=scripted_llm)

        interp2 = run(interpreter(state, runtime))
        state = apply_patch(state, interp2)
        assert state["interpreted_event"] == "SELECTION", (
            f"free-text tier reply not resolved — raw_analysis={state.get('raw_analysis')}"
        )
        assert state["extracted_entities"].get("selected_value") == "t10"

        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        state = apply_patch(state, run(cart_management(state, runtime)))

        # (2026-09-01, Option 1) : `quantity=60` a été donné au TOUR 1,
        # AVANT le choix du palier — doit redemander le nombre de paquets,
        # jamais compléter avec 60 bidons (600L) silencieusement.
        assert state["status"] == "WAITING_INPUT", (
            f"turn 3 (free text) must ask for the pack count — "
            f"status={state['status']} final_response={state.get('final_response')!r}"
        )
        assert to_tunnel_category(get_pending_interaction(state)) == "QUANTITY"
        assert state.get("vendor_selection_context", {}).get("resolved_tier_id") == "t10"

        state = _run_turn_boundary(state, runtime)

        state["normalized_text"] = "5"
        state["user_query"] = "5"
        plain_runtime = StubRuntime()
        state = apply_patch(state, run(interpreter(state, plain_runtime)))
        assert state["interpreted_event"] == "ANSWER", state.get("raw_analysis")
        state = apply_patch(state, run(memory_update(state, plain_runtime)))
        state = apply_patch(state, run(validator(state, plain_runtime)))
        state = apply_patch(state, run(cart_management(state, plain_runtime)))

        assert state["status"] == "COMPLETED", (
            f"turn 4 did not complete — status={state['status']} "
            f"final_response={state.get('final_response')!r}"
        )
        line = state["active_cart"][-1]
        assert line["tier_id"] == "t10"
        assert line["base_unit_quantity"] == 50.0  # 5 packs x 10L
