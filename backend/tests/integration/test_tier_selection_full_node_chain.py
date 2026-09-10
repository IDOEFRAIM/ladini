"""Full REAL node chain for the tier-selection bug — input_interpreter →
memory_update → validator → cart_management — across two turns, with state
merged between steps using the ACTUAL LangGraph reducer for each field
(introspected from `MarketAgentState`'s own `Annotated[...]` metadata), not a
naive `dict.update()`.

Why this exists: every previous test in this saga (including the checkpointer
round-trip and the memory.py stale-snapshot repro) called ONE node directly
with a hand-built state. That structurally cannot catch a bug in the HANDOFF
between nodes — e.g. the interpreter extracting something correctly, but a
different node consuming/clearing it before cart_management ever runs, or the
router sending the turn somewhere else entirely. This test runs the real
functions LangGraph would call, in the real order, with real reducer
semantics, for both turns of the exact live-reported scenario:

  Turn 1: "je veux 60 LITRE" → tier menu shown
  Turn 2: "1" → must resolve to the first tier and add to cart — MUST NOT
          re-show the same menu.
"""
from __future__ import annotations

import typing
from typing import Any, Dict

from ladini.graphs.agents.market_coach.core.state import MarketAgentState
from ladini.graphs.agents.market_coach.flows.buyer.cart import cart_management
from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from ladini.graphs.agents.market_coach.nodes.memory import memory_update
from ladini.graphs.agents.market_coach.nodes.validation import validator
from tests.conftest import StubRuntime, run
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
    set_pending_interaction,
    to_tunnel_category,
)

_HINTS = typing.get_type_hints(MarketAgentState, include_extras=True)


def _reducer_for(field: str):
    ann = _HINTS.get(field)
    if ann is None:
        return None
    # Annotated[T, reducer] -> __metadata__ = (reducer,)
    metadata = getattr(ann, "__metadata__", None)
    if not metadata:
        return None
    return metadata[0]


def apply_patch(state: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Merge `patch` into `state` using each field's REAL LangGraph reducer.

    This is the crux of what makes this test meaningful: `merge_dict` fields
    (transaction_payload, working_memory, extracted_entities...) merge
    key-by-key; `replace_value`/`replace_list` fields fully replace. A naive
    `state.update(patch)` (used by the existing demo script in
    `graph_builder.py`) gets this wrong for merge_dict fields and would mask
    exactly the class of bug this test exists to catch.
    """
    new_state = dict(state)
    for key, value in patch.items():
        reducer = _reducer_for(key)
        if reducer is None:
            new_state[key] = value
            continue
        new_state[key] = reducer(state.get(key), value)
    return new_state


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


class TestFullNodeChainTierSelection:
    def test_two_turns_through_real_nodes_resolves_the_tier(self, monkeypatch):
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

        # ---------------- TURN 1 : "je veux 60 LITRE" ----------------
        state: Dict[str, Any] = {
            "current_goal": "BUYER_ADD_TO_CART",
            "expected_input": "QUANTITY",
            "status": "WAITING_INPUT",
            "normalized_text": "je veux 60 litre",
            "user_query": "je veux 60 litre",
            "user_phone": "+22601479800",
            "transaction_payload": {"product": "lait"},
            "working_memory": {},
            "active_cart": [],
            **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"),
        }
        interp1 = run(interpreter(state, runtime))
        state = apply_patch(state, interp1)
        assert state["interpreted_event"] == "ANSWER", state.get("raw_analysis")

        mem1 = run(memory_update(state, runtime))
        state = apply_patch(state, mem1)

        val1 = run(validator(state, runtime))
        state = apply_patch(state, val1)

        cart1 = run(cart_management(state, runtime))
        state = apply_patch(state, cart1)

        assert state["status"] == "WAITING_INPUT"
        assert to_tunnel_category(get_pending_interaction(state)) == "SELECTION"
        assert "conditionnements" in (state.get("final_response") or ""), state.get("final_response")
        assert state.get("tier_selection_context", {}).get("tiers"), "tier menu never seeded tier_selection_context"

        # Simulate the turn-boundary nodes that ALSO run for real (ui_engine,
        # response_strategy, state_cleaner, post_response_cleanup) — this is
        # exactly the part earlier node-level tests skipped entirely.
        from ladini.graphs.agents.market_coach.nodes.cleaner import (
            state_cleaner_node,
        )
        from ladini.graphs.agents.market_coach.nodes.cleanup import (
            post_response_cleanup,
        )

        clean1 = run(state_cleaner_node(state, runtime))
        state = apply_patch(state, clean1)
        post1 = run(post_response_cleanup(state, runtime))
        state = apply_patch(state, post1)

        # Sanity: the claiming patch must have survived state_cleaner +
        # post_response_cleanup, exactly like vendor_selection_context does.
        assert state["working_memory"].get("available_mapping_kind") == "pricing_tier", (
            f"available_mapping_kind lost after turn-boundary cleanup nodes: "
            f"{state['working_memory']}"
        )
        assert state.get("available_mapping") == {}

        # ---------------- TURN 2 : "1" ----------------
        state["normalized_text"] = "1"
        state["user_query"] = "1"

        interp2 = run(interpreter(state, runtime))
        state = apply_patch(state, interp2)
        assert state["interpreted_event"] == "SELECTION", (
            f"interpreter did not classify a bare digit reply as SELECTION under "
            f"expected_input=SELECTION — raw_analysis={state.get('raw_analysis')}"
        )
        # (2026-09-01, contrat d'action structurée) : remplace l'ancien
        # `selection_index` générique — voir domain/selection_actions.py.
        assert state["extracted_entities"].get("agent_action") == "SELECT_PRICING_TIER"
        assert state["extracted_entities"].get("action_pricing_tier_id") == "t5"

        mem2 = run(memory_update(state, runtime))
        state = apply_patch(state, mem2)
        assert state["transaction_payload"].get("action_pricing_tier_id") == "t5", (
            "memory_update lost the structured action before cart_management "
            "could read it — THIS is the bug this whole test exists to catch"
        )

        val2 = run(validator(state, runtime))
        state = apply_patch(state, val2)

        cart2 = run(cart_management(state, runtime))
        state = apply_patch(state, cart2)

        # (2026-09-01, Option 1 — "aucune conversion silencieuse quantité
        # globale -> nombre de paquets") : `quantity=60` a été donné AVANT
        # même que le menu de paliers existe ("je veux 60 LITRE") — ce n'est
        # PAS une réponse à "combien de bidons de 5L ?". Turn 2 doit donc
        # ignorer ce 60 et reposer explicitement la question du nombre de
        # paquets, jamais compléter directement avec 60 bidons (300L,
        # incident réel vérifié : "30L" -> 30 bidons de 10L = 300L facturés
        # 27000 FCFA au lieu des ~3 bidons/2700 FCFA attendus).
        assert state["status"] == "WAITING_INPUT", (
            f"turn 2 must ask for the pack count, not silently reuse the "
            f"pre-tier flat quantity — status={state['status']} "
            f"final_response={state.get('final_response')!r}"
        )
        assert to_tunnel_category(get_pending_interaction(state)) == "QUANTITY"
        assert "5.0 L" in (state.get("final_response") or "")
        assert state.get("vendor_selection_context", {}).get("resolved_tier_id") == "t5"

        state = apply_patch(state, run(state_cleaner_node(state, runtime)))
        state = apply_patch(state, run(post_response_cleanup(state, runtime)))

        # ---------------- TURN 3 : "3" (réponse au nombre de paquets) -----
        state["normalized_text"] = "3"
        state["user_query"] = "3"

        interp3 = run(interpreter(state, runtime))
        state = apply_patch(state, interp3)
        assert state["interpreted_event"] == "ANSWER", state.get("raw_analysis")

        mem3 = run(memory_update(state, runtime))
        state = apply_patch(state, mem3)
        val3 = run(validator(state, runtime))
        state = apply_patch(state, val3)
        cart3 = run(cart_management(state, runtime))
        state = apply_patch(state, cart3)

        assert state["status"] == "COMPLETED", (
            f"turn 3 did not complete the cart insertion — status={state['status']} "
            f"final_response={state.get('final_response')!r}"
        )
        line = state["active_cart"][-1]
        assert line["tier_id"] == "t5"
        assert line["base_unit_quantity"] == 15.0  # 3 packs x 5L

    def test_free_text_reply_is_resolved_by_an_llm_informed_of_the_active_tiers(
        self, monkeypatch
    ):
        """(2026-08-30, refonte "LLM pilote la sélection de palier") — the
        deterministic free-text matcher that used to handle this case was
        removed: it broke on every phrasing variation. The LLM now resolves
        free-text tier replies itself, but — unlike the original
        "champignons" incident — it is INFORMED: the active tier list (with
        real `tier_id`s) is injected into its prompt. This test proves (a)
        the injected prompt actually contains the tier ids, and (b) a
        scripted LLM response using one of those ids resolves correctly
        end-to-end through cart_management, which independently validates
        the id against the live tier list before trusting it."""
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

        # Turn 1's exact quantity-extraction wording isn't what this test is
        # about (that's covered elsewhere) — seed a resolved quantity=4
        # directly, as if an earlier turn already established it, and go
        # straight through cart_management to reach the tier menu.
        state: Dict[str, Any] = {
            "current_goal": "BUYER_ADD_TO_CART",
            "expected_input": "QUANTITY",
            "status": "WAITING_INPUT",
            "normalized_text": "",
            "user_query": "",
            "user_phone": "+22601479800",
            "transaction_payload": {"product": "lait", "quantity": 4},
            "working_memory": {},
            "active_cart": [],
            **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"),
        }
        state = apply_patch(state, run(cart_management(state, seed_runtime)))
        assert to_tunnel_category(get_pending_interaction(state)) == "SELECTION"
        assert state.get("tier_selection_context", {}).get("tiers")

        from ladini.graphs.agents.market_coach.nodes.cleaner import (
            state_cleaner_node,
        )
        from ladini.graphs.agents.market_coach.nodes.cleanup import (
            post_response_cleanup,
        )

        state = apply_patch(state, run(state_cleaner_node(state, seed_runtime)))
        state = apply_patch(state, run(post_response_cleanup(state, seed_runtime)))

        # ---------------- TURN 2: free-text reply, no digit at all ------
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
        assert scripted_llm.calls == 1, "the LLM must actually be consulted this time"
        state = apply_patch(state, interp2)
        assert state["interpreted_event"] == "SELECTION", state.get("raw_analysis")
        assert state["raw_analysis"]["path"] == "llm"
        assert state["extracted_entities"].get("selected_value") == "t10"

        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        state = apply_patch(state, run(cart_management(state, runtime)))

        # (2026-09-01, Option 1) : `quantity=4` a été établi AVANT le choix
        # du palier (seedé directement dans transaction_payload) — ce n'est
        # pas une réponse à "combien de bidons de 10L ?". Doit redemander,
        # jamais compléter avec 4 bidons (40L) silencieusement.
        assert state["status"] == "WAITING_INPUT", state.get("final_response")
        assert to_tunnel_category(get_pending_interaction(state)) == "QUANTITY"
        assert "10.0 L" in (state.get("final_response") or "")

        state = apply_patch(state, run(state_cleaner_node(state, runtime)))
        state = apply_patch(state, run(post_response_cleanup(state, runtime)))

        state["normalized_text"] = "5"
        state["user_query"] = "5"
        plain_runtime = StubRuntime()
        state = apply_patch(state, run(interpreter(state, plain_runtime)))
        assert state["interpreted_event"] == "ANSWER", state.get("raw_analysis")
        state = apply_patch(state, run(memory_update(state, plain_runtime)))
        state = apply_patch(state, run(validator(state, plain_runtime)))
        state = apply_patch(state, run(cart_management(state, plain_runtime)))

        assert state["status"] == "COMPLETED", state.get("final_response")
        line = state["active_cart"][-1]
        assert line["tier_id"] == "t10"
        assert line["base_unit_quantity"] == 50.0  # 5 packs x 10L

    def test_tier_menu_context_is_actually_injected_into_the_llm_prompt(
        self, monkeypatch
    ):
        """Proves the mechanism itself, not just its effect: the tier list
        (with real tier_ids) must be present in the user prompt sent to the
        LLM whenever a tier menu is active — this is what was missing
        during the "champignons" incident."""
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

        state: Dict[str, Any] = {
            "current_goal": "BUYER_ADD_TO_CART",
            "expected_input": "QUANTITY",
            "status": "WAITING_INPUT",
            "normalized_text": "",
            "user_query": "",
            "user_phone": "+22601479800",
            "transaction_payload": {"product": "lait", "quantity": 4},
            "working_memory": {},
            "active_cart": [],
            **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"),
        }
        state = apply_patch(state, run(cart_management(state, seed_runtime)))

        from ladini.graphs.agents.market_coach.nodes.cleaner import (
            state_cleaner_node,
        )
        from ladini.graphs.agents.market_coach.nodes.cleanup import (
            post_response_cleanup,
        )

        state = apply_patch(state, run(state_cleaner_node(state, seed_runtime)))
        state = apply_patch(state, run(post_response_cleanup(state, seed_runtime)))

        state["normalized_text"] = "un bidon de lait"
        state["user_query"] = "un bidon de lait"

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
        captured = {}
        original_create = scripted_llm.create

        def _capturing_create(**kwargs):
            captured["messages"] = kwargs.get("messages")
            return original_create(**kwargs)

        scripted_llm.create = _capturing_create

        run(interpreter(state, runtime))

        user_message = next(
            m["content"] for m in captured["messages"] if m["role"] == "user"
        )
        assert "t5" in user_message and "t10" in user_message, (
            f"the active tier ids were not injected into the LLM prompt: {user_message!r}"
        )
        # (2026-09-01, contrat d'action structurée) : remplace l'ancien
        # `palier_conditionnement_actif` texte libre — voir
        # domain/selection_actions.py::tier_menu_prompt_block.
        assert "action_structuree_attendue" in user_message
        assert "SELECT_PRICING_TIER" in user_message
