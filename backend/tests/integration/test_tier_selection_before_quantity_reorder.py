"""Real node-chain tests for the "palier avant quantité" reorder
(2026-08-30, explicit user request): the buyer must see the packaging/tier
menu as soon as the product is identified — BEFORE being asked for a raw
quantity — choose a tier, and only THEN be asked how many of that tier.

Also covers the companion FSM hardening: an unmatched free-text reply to
an active tier menu must NEVER reach the LLM (that's the general mechanism
behind the "champignons" hallucination bug — a confident but wrong
NEW_TASK/BUYER_REQUEST classification breaks `SELECTION` out of the tunnel
since it's a soft slot in `tunnel_manager.py`), and `memory_update` must
not purge the in-progress vendor/tier context on a conflicting product
mention while a selection tunnel is active.

Uses the same `apply_patch` (real LangGraph reducer per field) helper
established in `test_tier_selection_full_node_chain.py`.
"""
from __future__ import annotations

from typing import Any, Dict

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)
from ladini.graphs.agents.market_coach.flows.buyer.cart import cart_management
from ladini.graphs.agents.market_coach.interpreter.goal_planner import (
    goal_planner,
)
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


def _base_state(text: str) -> Dict[str, Any]:
    return {
        "current_goal": None,
        "expected_input": "NONE",
        "status": "PROCESSING",
        "normalized_text": text,
        "user_query": text,
        "user_phone": "+22601479800",
        "transaction_payload": {"product": "lait"},
        "working_memory": {},
        "active_cart": [],
    }


class TestTierBeforeQuantitySingleVendor:
    def test_product_only_message_shows_tier_menu_before_asking_quantity(self, monkeypatch):
        import ladini.graphs.agents.market_coach.services.domain.cart_service as cart_service_mod

        async def _fake_resolve_vendors(self, phone, product_name):
            return [_tiered_vendor()], False

        monkeypatch.setattr(
            cart_service_mod.CartDomainService,
            "resolve_product_vendors",
            _fake_resolve_vendors,
        )

        runtime = StubRuntime()
        state = _base_state("je veux du lait")
        state["current_goal"] = "BUYER_ADD_TO_CART"

        result = run(cart_management(state, runtime))

        assert to_tunnel_category(get_pending_interaction(result)) == "SELECTION", result.get("final_response")
        assert "conditionnements" in (result.get("final_response") or "")
        assert "quantité" not in (result.get("final_response") or "").lower(), (
            "quantity must not be asked before the tier is chosen"
        )
        tier_ctx = result.get("tier_selection_context")
        assert tier_ctx and tier_ctx.get("tiers"), "tier menu never shown"
        assert result.get("vendor_selection_context", {}).get("chosen_vendor"), (
            "single vendor must be pre-resolved into vendor_selection_context "
            "so the follow-up turn routes through vendor_ctx_active"
        )

    def test_digit_reply_asks_quantity_referencing_the_chosen_tier_then_completes(self, monkeypatch):
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

        state = _base_state("je veux du lait")
        state["current_goal"] = "BUYER_ADD_TO_CART"
        state = apply_patch(state, run(cart_management(state, runtime)))
        state = _run_turn_boundary(state, runtime)

        # Reply "2" -> picks the 10L tier. Quantity is still unknown -> must
        # ask for it, referencing the CHOSEN packaging, not a bare number.
        state["normalized_text"] = "2"
        state["user_query"] = "2"
        state = apply_patch(state, run(interpreter(state, runtime)))
        assert state["interpreted_event"] == "SELECTION"
        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        state = apply_patch(state, run(cart_management(state, runtime)))

        assert state["status"] == "WAITING_INPUT"
        assert to_tunnel_category(get_pending_interaction(state)) == "QUANTITY"
        assert "10.0 L" in (state.get("final_response") or "") or "10 L" in (
            state.get("final_response") or ""
        ), state.get("final_response")
        assert state.get("vendor_selection_context", {}).get("resolved_tier_id") == "t10", (
            "resolved tier must be persisted across the quantity-asking turn"
        )
        state = _run_turn_boundary(state, runtime)

        # Reply with the quantity -> must complete directly using the
        # persisted tier, no re-ask, no re-shown menu.
        state["normalized_text"] = "3"
        state["user_query"] = "3"
        state = apply_patch(state, run(interpreter(state, runtime)))
        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        state = apply_patch(state, run(cart_management(state, runtime)))

        assert state["status"] == "COMPLETED", (
            f"status={state['status']} final_response={state.get('final_response')!r}"
        )
        line = state["active_cart"][-1]
        assert line["tier_id"] == "t10"
        assert line["base_unit_quantity"] == 30.0  # 3 packs x 10L

    def test_ordinal_reply_resolves_the_tier(self, monkeypatch):
        """(2026-08-30, refonte "LLM pilote la sélection de palier") —
        ordinal resolution ("le deuxième") is now the LLM's job, informed by
        the injected tier list (rule 4bis). Scripted here since this test
        isn't about proving injection itself (see
        `test_tier_menu_context_is_actually_injected_into_the_llm_prompt`
        for that) — it proves the DOWNSTREAM handling of an LLM-resolved
        ordinal is correct."""
        import ladini.graphs.agents.market_coach.services.domain.cart_service as cart_service_mod
        from tests.conftest import ScriptedLLM

        async def _fake_resolve_vendors(self, phone, product_name):
            return [_tiered_vendor()], False

        monkeypatch.setattr(
            cart_service_mod.CartDomainService,
            "resolve_product_vendors",
            _fake_resolve_vendors,
        )

        interpreter = make_input_interpreter("BUYER")
        seed_runtime = StubRuntime()

        state = _base_state("je veux du lait")
        state["current_goal"] = "BUYER_ADD_TO_CART"
        state = apply_patch(state, run(cart_management(state, seed_runtime)))
        state = _run_turn_boundary(state, seed_runtime)

        state["normalized_text"] = "le deuxième"
        state["user_query"] = "le deuxième"
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
        state = apply_patch(state, run(interpreter(state, runtime)))
        assert state["interpreted_event"] == "SELECTION", state.get("raw_analysis")
        assert state["extracted_entities"].get("selected_value") == "t10"

        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        state = apply_patch(state, run(cart_management(state, runtime)))

        assert to_tunnel_category(get_pending_interaction(state)) == "QUANTITY"
        assert state.get("vendor_selection_context", {}).get("resolved_tier_id") == "t10"

    def test_product_and_quantity_given_upfront_never_becomes_a_silent_pack_count(
        self, monkeypatch
    ):
        """(2026-09-01, Option 1 — incident réel vérifié) : quand la
        quantité est donnée AVANT même que le menu de paliers existe
        ("je veux 60 litres de lait"), ce nombre est une quantité GLOBALE
        (litres), jamais un nombre de paquets. Un acheteur ayant tapé
        "30 litre" puis choisi le palier 10L se voyait auparavant facturer
        30 BIDONS de 10L (300L, 27000 FCFA) au lieu des ~3 bidons attendus
        — la quantité globale pré-palier ne doit plus JAMAIS être réutilisée
        silencieusement comme nombre de paquets ; le système doit reposer
        explicitement la question ("Combien de bidons de 10L ?")."""
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

        state = _base_state("je veux 60 litres de lait")
        state["current_goal"] = "BUYER_ADD_TO_CART"
        state["transaction_payload"] = {"product": "lait", "quantity": 60, "unit": "LITRE"}
        state = apply_patch(state, run(cart_management(state, runtime)))
        assert to_tunnel_category(get_pending_interaction(state)) == "SELECTION"
        assert state.get("vendor_selection_context", {}).get("requested_quantity") == 60
        state = _run_turn_boundary(state, runtime)

        state["normalized_text"] = "2"
        state["user_query"] = "2"
        state = apply_patch(state, run(interpreter(state, runtime)))
        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        state = apply_patch(state, run(cart_management(state, runtime)))

        assert state["status"] == "WAITING_INPUT", (
            f"the pre-tier flat quantity (60) must never be silently reused "
            f"as a pack count — status={state['status']} "
            f"final_response={state.get('final_response')!r}"
        )
        assert to_tunnel_category(get_pending_interaction(state)) == "QUANTITY"
        assert "10.0 L" in (state.get("final_response") or "")
        assert state.get("vendor_selection_context", {}).get("requested_quantity") is None, (
            "the stale pre-tier quantity must be cleared, not just ignored, "
            "so it cannot resurface on a later turn"
        )
        state = _run_turn_boundary(state, runtime)

        state["normalized_text"] = "4"
        state["user_query"] = "4"
        state = apply_patch(state, run(interpreter(state, runtime)))
        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        state = apply_patch(state, run(cart_management(state, runtime)))

        assert state["status"] == "COMPLETED", state.get("final_response")
        line = state["active_cart"][-1]
        assert line["tier_id"] == "t10"
        assert line["base_unit_quantity"] == 40.0  # 4 packs x 10L


class TestTierBeforeQuantityMultiVendor:
    def test_full_sequence_product_pick_vendor_pick_tier_then_quantity(self, monkeypatch):
        import ladini.graphs.agents.market_coach.services.domain.cart_service as cart_service_mod

        async def _fake_resolve_vendors(self, phone, product_name):
            return [_tiered_vendor(), _other_vendor()], True

        monkeypatch.setattr(
            cart_service_mod.CartDomainService,
            "resolve_product_vendors",
            _fake_resolve_vendors,
        )

        interpreter = make_input_interpreter("BUYER")
        runtime = StubRuntime()

        # Turn 1: product only, no quantity -> vendor menu (no tier menu
        # yet, tiers are per-vendor and no vendor is chosen).
        state = _base_state("je veux du lait")
        state["current_goal"] = "BUYER_ADD_TO_CART"
        state = apply_patch(state, run(cart_management(state, runtime)))
        assert to_tunnel_category(get_pending_interaction(state)) == "SELECTION"
        assert state.get("vendor_selection_context", {}).get("vendors")
        assert not state.get("vendor_selection_context", {}).get("chosen_vendor")
        state = _run_turn_boundary(state, runtime)

        # Turn 2: pick vendor #1 (has tiers) -> tier menu shown directly,
        # still no quantity asked.
        state["normalized_text"] = "1"
        state["user_query"] = "1"
        state = apply_patch(state, run(interpreter(state, runtime)))
        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        state = apply_patch(state, run(cart_management(state, runtime)))
        assert to_tunnel_category(get_pending_interaction(state)) == "SELECTION"
        assert state.get("tier_selection_context", {}).get("tiers")
        assert "quantité" not in (state.get("final_response") or "").lower()
        state = _run_turn_boundary(state, runtime)

        # Turn 3: pick tier #2 -> ask quantity for THAT tier.
        state["normalized_text"] = "2"
        state["user_query"] = "2"
        state = apply_patch(state, run(interpreter(state, runtime)))
        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        state = apply_patch(state, run(cart_management(state, runtime)))
        assert to_tunnel_category(get_pending_interaction(state)) == "QUANTITY"
        assert state.get("vendor_selection_context", {}).get("resolved_tier_id") == "t10"
        state = _run_turn_boundary(state, runtime)

        # Turn 4: give the quantity -> completes.
        state["normalized_text"] = "4"
        state["user_query"] = "4"
        state = apply_patch(state, run(interpreter(state, runtime)))
        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        state = apply_patch(state, run(cart_management(state, runtime)))

        assert state["status"] == "COMPLETED", state.get("final_response")
        line = state["active_cart"][-1]
        assert line["tier_id"] == "t10"
        assert line["base_unit_quantity"] == 40.0  # 4 packs x 10L


class TestValidatorRouterDoesNotBlockTheReorder:
    """Real live regression (2026-08-30): the two tests above proved the
    reorder works when `cart_management` is entered directly — but on the
    ACTUAL first WhatsApp message, `validator` runs BEFORE `cart_management`
    and independently gates on `INTENT_CONFIG["BUYER_ADD_TO_CART"]["required"]`.
    With `quantity`/`unit` still listed there, `validator` itself answered
    "quelle quantité ?" and `tunnel_manager.is_cart_routeable()` (called from
    `DomainRouter.decide`) refused to route to `cart_management` at all while
    `missing_fields` was non-empty — making the entire reorder unreachable on
    the very message it was built for. This runs the REAL validator +
    DomainRouter.decide + cart_management chain to prove the fix, exactly
    the layer the earlier tests in this file skipped."""

    def test_product_only_message_routes_to_cart_and_shows_tier_menu(self, monkeypatch):
        import ladini.graphs.agents.market_coach.services.domain.cart_service as cart_service_mod
        from ladini.graphs.agents.market_coach.core.router import DomainRouter

        async def _fake_resolve_vendors(self, phone, product_name):
            return [_tiered_vendor()], False

        monkeypatch.setattr(
            cart_service_mod.CartDomainService,
            "resolve_product_vendors",
            _fake_resolve_vendors,
        )

        runtime = StubRuntime()
        state = _base_state("je veux acheter du lait")
        state["current_goal"] = "BUYER_ADD_TO_CART"

        val = run(validator(state, runtime))
        assert val.get("missing_fields") == [], (
            f"validator still blocks on a field cart_management already "
            f"handles itself: {val.get('missing_fields')}"
        )
        state = apply_patch(state, val)

        router = DomainRouter.build()
        target = router.decide(state)
        assert target == "to_cart", (
            f"router refused to route to cart_management (target={target}) — "
            f"the tier-before-quantity reorder is unreachable from here"
        )

        result = run(cart_management(state, runtime))
        assert to_tunnel_category(get_pending_interaction(result)) == "SELECTION", result.get("final_response")
        assert "conditionnements" in (result.get("final_response") or "")
        assert "quantité" not in (result.get("final_response") or "").lower()
        assert result.get("tier_selection_context", {}).get("tiers")


class TestTierTunnelFsmLock:
    """(2026-08-30, refonte "LLM pilote la sélection de palier") — the
    deterministic pre-LLM lock was removed per explicit user request: it
    blocked the LLM from ever seeing a tier-tunnel reply, which is exactly
    what the user asked to stop doing. The LLM now sees every reply, but
    informed (the active tier list is injected into its prompt — see
    `test_tier_selection_full_node_chain.py::test_tier_menu_context_is_actually_injected_into_the_llm_prompt`).
    This test proves the tunnel still survives an unmatched reply when the
    (informed) LLM correctly returns UNKNOWN, per the new system-prompt rule
    4bis — the protection now lives in the prompt + `goal_planner`'s
    existing RÈGLE 2, not in a pre-LLM block."""

    def test_unmatched_free_text_llm_response_stays_in_tunnel_with_no_purge(
        self, monkeypatch
    ):
        import ladini.graphs.agents.market_coach.services.domain.cart_service as cart_service_mod
        from tests.conftest import ScriptedLLM

        async def _fake_resolve_vendors(self, phone, product_name):
            return [_tiered_vendor()], False

        monkeypatch.setattr(
            cart_service_mod.CartDomainService,
            "resolve_product_vendors",
            _fake_resolve_vendors,
        )

        interpreter = make_input_interpreter("BUYER")
        seed_runtime = StubRuntime()

        state = _base_state("je veux du lait")
        state["current_goal"] = "BUYER_ADD_TO_CART"
        state = apply_patch(state, run(cart_management(state, seed_runtime)))
        assert to_tunnel_category(get_pending_interaction(state)) == "SELECTION"
        state = _run_turn_boundary(state, seed_runtime)

        # A free-text reply that matches no tier. The LLM is now consulted
        # (never blocked) — scripted here to return UNKNOWN, exactly what
        # the new system-prompt rule 4bis instructs it to do when nothing
        # in the active tier list matches confidently.
        state["normalized_text"] = "je voudrais des champignons"
        state["user_query"] = "je voudrais des champignons"

        scripted_llm = ScriptedLLM(
            {
                "interpreted_event": "UNKNOWN",
                "detected_intent": "UNKNOWN",
                "interpreter_confidence": 1.0,
                "validation_status": "VALID",
                "extracted_entities": {},
            }
        )
        runtime = StubRuntime(llm=scripted_llm)

        interp = run(interpreter(state, runtime))
        assert scripted_llm.calls == 1, "the LLM must be consulted, never blocked"
        assert interp["interpreted_event"] == "UNKNOWN", interp.get("raw_analysis")
        assert interp["raw_analysis"]["path"] == "llm"
        state = apply_patch(state, interp)

        state = apply_patch(state, run(goal_planner(state, runtime)))
        assert state["current_goal"] == "BUYER_ADD_TO_CART", (
            "goal_planner let the tunnel break out on an unmatched tier reply"
        )
        assert state["goal_status"] == "WAITING_INPUT"

        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        state = apply_patch(state, run(cart_management(state, runtime)))

        assert state["status"] == "WAITING_INPUT"
        assert to_tunnel_category(get_pending_interaction(state)) == "SELECTION"
        assert "conditionnements" in (state.get("final_response") or "")
        assert state.get("vendor_selection_context", {}).get("chosen_vendor"), (
            "vendor context must survive an unmatched tier reply"
        )
        assert state.get("tier_selection_context", {}).get("tiers"), (
            "tier context must survive an unmatched tier reply"
        )


class TestMemoryUpdateTunnelGuard:
    def test_conflicting_product_entity_does_not_purge_context_during_tier_tunnel(self):
        runtime = StubRuntime()
        state: Dict[str, Any] = {
            "current_goal": "BUYER_ADD_TO_CART",
            "expected_input": "SELECTION",
            "status": "WAITING_INPUT",
            "interpreted_event": "UNKNOWN",
            "detected_intent": "UNKNOWN",
            "normalized_text": "je voudrais des champignons",
            "user_query": "je voudrais des champignons",
            "user_phone": "+22601479800",
            "transaction_payload": {"product": "lait"},
            # Simulates a stray extraction (e.g. a misfiring upstream
            # heuristic) carrying a conflicting "product" entity while the
            # tier tunnel is active.
            "extracted_entities": {"product": "champignons"},
            "working_memory": {"available_mapping_kind": "pricing_tier"},
            "vendor_selection_context": {
                "product": "lait",
                "vendors": [_tiered_vendor()],
                "chosen_vendor": _tiered_vendor(),
            },
            "tier_selection_context": {
                "product_id": "P-LAIT",
                "tiers": _tiered_vendor()["pricing_tiers"],
            },
            "stable_entities": {"product": "lait", "quantity": 60},
            "active_cart": [],
        }

        result = run(memory_update(state, runtime))

        assert result["transaction_payload"].get("product") == "lait", (
            "product slot must NOT be overwritten while a selection tunnel is active"
        )
        # memory.py only ever sets "vendor_selection_context" in its patch
        # when `clear_vendor_ctx` fired — the guard must prevent that, so
        # the key must be entirely absent (not merely None/empty).
        assert "vendor_selection_context" not in result, (
            f"vendor context was purged despite the active tier tunnel: {result.get('vendor_selection_context')!r}"
        )
