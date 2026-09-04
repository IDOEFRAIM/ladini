"""`nodes/cognitive.py` — garde-fou cognitif (protection de tunnel, abandon
après N échecs, report d'entités stables) + orchestrateur (phase du cycle
perceive→think→decide→act→observe→reason)."""
from __future__ import annotations

import pytest

from agriconnect.graphs.agents.market_coach.nodes.cognitive import (
    _build_proactive_hint,
    _entity_carry_forward,
    _should_trigger_disambiguation,
    cognitive_guard,
    cognitive_orchestrator,
)
from tests.conftest import make_state, run


# =====================================================================
# _build_proactive_hint
# =====================================================================

class TestBuildProactiveHint:
    def test_none_without_a_goal(self):
        assert _build_proactive_hint(None, {"pct": 50}, {}) is None

    def test_none_without_progress(self):
        assert _build_proactive_hint("SALES_PUBLISH_PRODUCT", None, {}) is None

    def test_zero_percent_announces_new_operation(self):
        hint = _build_proactive_hint("SALES_PUBLISH_PRODUCT", {"pct": 0, "remaining": ["price"]}, {})
        assert "Nouvelle opération" in hint

    def test_hundred_percent_announces_ready_for_confirmation(self):
        hint = _build_proactive_hint("SALES_PUBLISH_PRODUCT", {"pct": 100, "remaining": []}, {})
        assert "confirmation" in hint

    def test_one_remaining_field_names_it(self):
        hint = _build_proactive_hint("SALES_PUBLISH_PRODUCT", {"pct": 66, "remaining": ["price"]}, {})
        assert "Plus qu'une info" in hint

    def test_multiple_remaining_fields_shows_percentage(self):
        hint = _build_proactive_hint("SALES_PUBLISH_PRODUCT", {"pct": 33, "remaining": ["price", "quantity"]}, {})
        assert "33%" in hint
        assert "2" in hint


# =====================================================================
# _entity_carry_forward
# =====================================================================

class TestEntityCarryForward:
    def test_none_outside_a_tunnel(self):
        state = make_state(stable_entities={"product": "mais"}, extracted_entities={})
        assert _entity_carry_forward(state, "SALES_PUBLISH_PRODUCT", False) is None

    def test_none_without_a_current_goal(self):
        state = make_state(stable_entities={"product": "mais"}, extracted_entities={})
        assert _entity_carry_forward(state, None, True) is None

    def test_carries_missing_product_unit_zone_from_stable(self):
        state = make_state(
            stable_entities={"product": "mais", "unit": "KG", "zone_name": "Ouaga"},
            extracted_entities={},
        )
        result = _entity_carry_forward(state, "SALES_PUBLISH_PRODUCT", True)
        assert result == {"product": "mais", "unit": "KG", "zone_name": "Ouaga"}

    def test_does_not_overwrite_already_extracted_entities(self):
        state = make_state(
            stable_entities={"product": "mais"},
            extracted_entities={"product": "riz"},
        )
        result = _entity_carry_forward(state, "SALES_PUBLISH_PRODUCT", True)
        assert result is None or result.get("product") == "riz"

    def test_none_when_nothing_to_carry(self):
        state = make_state(stable_entities={}, extracted_entities={"product": "mais"})
        assert _entity_carry_forward(state, "SALES_PUBLISH_PRODUCT", True) is None


# =====================================================================
# _should_trigger_disambiguation
# =====================================================================

class TestShouldTriggerDisambiguation:
    def test_multiple_intents_low_confidence_triggers(self):
        competition = [{"intent": "A"}, {"intent": "B"}]
        assert _should_trigger_disambiguation(competition, 0.5) is True

    def test_multiple_intents_high_confidence_does_not_trigger(self):
        competition = [{"intent": "A"}, {"intent": "B"}]
        assert _should_trigger_disambiguation(competition, 0.9) is False

    def test_single_intent_never_triggers(self):
        competition = [{"intent": "A"}, {"intent": "A"}]
        assert _should_trigger_disambiguation(competition, 0.1) is False

    def test_empty_competition_never_triggers(self):
        assert _should_trigger_disambiguation([], 0.1) is False


# =====================================================================
# cognitive_guard
# =====================================================================

class TestCognitiveGuardInterruption:
    def test_new_task_with_different_intent_suspends_the_current_goal(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="NEW_TASK",
            detected_intent="BUYER_REQUEST",
            expected_input="QUANTITY",
        )
        result = run(cognitive_guard(state, None))
        assert result["interpreted_event"] == "INTERRUPTION"
        assert result["cognitive_decision"]["action"] == "suspend_current_goal"

    def test_new_task_with_the_same_intent_as_current_goal_does_not_interrupt(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="NEW_TASK",
            detected_intent="SALES_PUBLISH_PRODUCT",
            expected_input="QUANTITY",
        )
        result = run(cognitive_guard(state, None))
        assert result.get("interpreted_event") != "INTERRUPTION"

    def test_new_task_with_unknown_intent_does_not_interrupt(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="NEW_TASK",
            detected_intent="UNKNOWN",
            expected_input="QUANTITY",
        )
        result = run(cognitive_guard(state, None))
        assert result.get("interpreted_event") != "INTERRUPTION"


class TestCognitiveGuardUnknownInTunnel:
    def test_low_retry_count_triggers_recovery(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="UNKNOWN",
            expected_input="QUANTITY",
            retry_count=0,
        )
        result = run(cognitive_guard(state, None))
        assert result["cognitive_decision"]["action"] == "recover_active_tunnel"
        assert result["response_strategy"] == "RECOVERY"
        assert result["current_goal"] == "SALES_PUBLISH_PRODUCT"

    def test_max_retries_abandons_the_tunnel_with_a_full_reset(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="UNKNOWN",
            expected_input="QUANTITY",
            retry_count=2,
        )
        result = run(cognitive_guard(state, None))
        assert result["cognitive_decision"]["action"] == "abandon_tunnel_max_retries"
        assert result["current_goal"] is None
        assert result["transaction_payload"] == {"__reset__": True}
        assert result["response_strategy"] == "CLARIFICATION"

    def test_max_retries_abandon_also_clears_stale_producer_mini_state_machines(self):
        """Chantier résilience 2026-08 : `bid_phase`/`update_phase` (mini
        machines à états auto-suffisantes, flows/producer/auctions.py +
        flow.py) vivent exclusivement dans working_memory, jamais touché par
        ce reset avant ce fix — un abandon en plein milieu d'une confirmation
        d'offre laissait `bid_phase="CONFIRM"` stale, et relancer le même
        goal peu après pouvait réafficher un récap périmé au lieu de
        redémarrer proprement."""
        state = make_state(
            current_goal="MARKET_BROWSE_REQUESTS",
            interpreted_event="UNKNOWN",
            expected_input="CONFIRMATION",
            retry_count=2,
            working_memory={
                "bid_phase": "CONFIRM",
                "pending_bid_auction": "a1",
                "pending_bid_price": 250,
                "update_phase": "COLLECT",
                "update_cycle_id": "c1",
                "auction_brief": {"a1": {"product": "mais"}},  # doit survivre
            },
        )
        result = run(cognitive_guard(state, None))
        wm = result["working_memory"]
        assert wm["bid_phase"] is None
        assert wm["pending_bid_auction"] is None
        assert wm["pending_bid_price"] is None
        assert wm["update_phase"] is None
        assert wm["update_cycle_id"] is None
        assert wm["auction_brief"] == {"a1": {"product": "mais"}}

    def test_max_retries_abandon_also_clears_stale_buyer_gps_stage_flags(self):
        """Même bug, côté acheteur (incident réel +22601479800, 2026-09-02) :
        `preorder_workflow.gps_stage`/`gps_default` (flows/buyer/preorder.py)
        et `working_memory.winner_gps_stage` (flows/buyer/order_tracking.py)
        sont la même famille de mini-état auto-suffisant que
        `bid_phase`/`update_phase` ci-dessus, mais vivaient hors de portée du
        reset : `pending_interaction` (PROVIDE_LOCATION) était bien effacé à
        l'abandon, mais `gps_stage=True` restait collé (merge_dict ne s'auto-
        efface jamais). Reprendre le même goal plus tard retombait alors sur
        `resolve_gps_stage` sans aucun `pending_interaction` actif pour le
        justifier — exactement la désynchronisation observée en prod."""
        from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
            InteractionKind,
            set_pending_interaction,
        )

        state = make_state(
            current_goal="BUYER_PREORDER_INIT",
            interpreted_event="UNKNOWN",
            retry_count=2,
            working_memory={"winner_gps_stage": True},
            preorder_workflow={
                "phase": "PREORDER_DRAFTED",
                "preorder_id": "abc123",
                "gps_stage": True,
                "gps_default": {"lat": 12.35, "lon": -1.5},
            },
            **set_pending_interaction(
                InteractionKind.PROVIDE_LOCATION, context_ref="confirmation"
            ),
        )
        result = run(cognitive_guard(state, None))

        assert result["cognitive_decision"]["action"] == "abandon_tunnel_max_retries"
        assert result["working_memory"]["winner_gps_stage"] is None
        assert result["preorder_workflow"]["gps_stage"] is None
        assert result["preorder_workflow"]["gps_default"] is None
        assert result["pending_interaction"] is None

    def test_unknown_event_outside_a_tunnel_does_not_trigger_this_branch(self):
        state = make_state(current_goal=None, interpreted_event="UNKNOWN", expected_input="NONE")
        result = run(cognitive_guard(state, None))
        assert result["cognitive_decision"]["action"] == "continue"

    def test_a_shared_location_in_tunnel_is_not_treated_as_an_unknown_event(self):
        """Bug réel (2026-08-13) : un partage de position WhatsApp natif n'a
        pas de texte à classifier — l'interprète renvoie event=UNKNOWN pour
        ces tours. Avant ce fix, ÇA incrémentait retry_count et forçait
        response_strategy=RECOVERY, qui court-circuite tout le graphe
        directement vers la réponse (nodes/routing `_route_after_clarification`)
        SANS jamais atteindre le resolver — un point GPS valide au stade
        `finalize_winner`/`create_preorder` tombait donc systématiquement sur
        le message générique "Je n'ai pas bien saisi", même après N tentatives.
        Voir [[gps-delivery-burkina-faso-2026-08]]."""
        state = make_state(
            current_goal="BUYER_PREORDER_INIT",
            interpreted_event="UNKNOWN",
            expected_input="CONFIRMATION",
            retry_count=0,
            location_shared=True,
        )
        result = run(cognitive_guard(state, None))
        assert result["cognitive_decision"]["action"] == "continue"
        assert result.get("response_strategy") != "RECOVERY"
        assert "current_goal" not in result, "le tunnel ne doit pas être touché"

    def test_a_shared_location_never_triggers_tunnel_abandonment_even_at_max_retries(self):
        state = make_state(
            current_goal="BUYER_PREORDER_INIT",
            interpreted_event="UNKNOWN",
            expected_input="CONFIRMATION",
            retry_count=5,
            location_shared=True,
        )
        result = run(cognitive_guard(state, None))
        assert result["cognitive_decision"]["action"] == "continue"
        assert result.get("response_strategy") != "CLARIFICATION"


class TestCognitiveGuardEntityCarryAndProgress:
    def test_carried_entities_are_reflected_in_updates_and_decision(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="QUANTITY",
            interpreted_event="ANSWER",
            stable_entities={"product": "mais"},
            extracted_entities={},
        )
        result = run(cognitive_guard(state, None))
        assert result["extracted_entities"]["product"] == "mais"
        assert result["cognitive_decision"]["entity_carry_forward"] is True

    def test_progress_and_proactive_hint_are_computed_for_a_known_goal(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="QUANTITY",
            interpreted_event="ANSWER",
            transaction_payload={},
        )
        result = run(cognitive_guard(state, None))
        assert "conversation_progress" in result
        assert result["cognitive_decision"]["action"] == "continue"

    def test_lexical_disambiguation_candidates_are_added_to_competition(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.nodes.cognitive as mod

        monkeypatch.setattr(
            mod, "_detect_disambiguation_candidates",
            lambda text, role: {"id": "trigger1", "candidates": ["INTENT_A", "INTENT_B"]},
        )
        state = make_state(interpreted_event="NEW_TASK", detected_intent="UNKNOWN")
        result = run(cognitive_guard(state, None))
        sources = [c["source"] for c in result["intent_competition"]]
        assert "lexical_disambiguation" in sources

    def test_default_action_is_continue_with_no_special_context(self):
        state = make_state(current_goal=None, interpreted_event="NEW_TASK", detected_intent="UNKNOWN")
        result = run(cognitive_guard(state, None))
        assert result["cognitive_decision"]["action"] == "continue"


# =====================================================================
# cognitive_orchestrator
# =====================================================================

class TestCognitiveOrchestrator:
    def test_onboarding_state_short_circuits(self):
        state = make_state(is_onboarding=True, interpreted_event="NEW_TASK")
        result = run(cognitive_orchestrator(state, None))
        assert result["cognitive_decision"]["next_step"] == "onboarding"
        assert result["should_replan"] is False

    def test_clarification_strategy_maps_to_respond(self):
        state = make_state(response_strategy="CLARIFICATION")
        result = run(cognitive_orchestrator(state, None))
        assert result["cognitive_decision"]["phase"] == "reason"
        assert result["cognitive_decision"]["next_step"] == "respond"

    def test_recovery_strategy_maps_to_respond(self):
        state = make_state(response_strategy="RECOVERY")
        result = run(cognitive_orchestrator(state, None))
        assert result["cognitive_decision"]["next_step"] == "respond"

    def test_interruption_event_maps_to_replan(self):
        state = make_state(interpreted_event="INTERRUPTION")
        result = run(cognitive_orchestrator(state, None))
        assert result["cognitive_decision"]["phase"] == "decide"
        assert result["cognitive_decision"]["next_step"] == "replan"
        assert result["should_replan"] is True

    def test_intent_competition_triggers_clarify(self):
        state = make_state(
            intent_competition=[{"intent": "A"}, {"intent": "B"}],
            interpreter_confidence=0.3,
        )
        result = run(cognitive_orchestrator(state, None))
        assert result["cognitive_decision"]["next_step"] == "clarify"
        assert result["cognitive_decision"]["reason"] == "intent_competition"

    @pytest.mark.parametrize("event", ["ANSWER", "UPDATE", "SELECTION", "CONFIRM", "REJECT"])
    def test_active_tunnel_events_continue_the_tunnel(self, event):
        state = make_state(current_goal="SALES_PUBLISH_PRODUCT", expected_input="QUANTITY", interpreted_event=event)
        result = run(cognitive_orchestrator(state, None))
        assert result["cognitive_decision"]["next_step"] == "continue_tunnel"

    def test_a_shared_location_in_tunnel_continues_the_tunnel_even_with_event_unknown(self):
        """Un partage de position n'a pas d'event classifiable (event=UNKNOWN)
        mais doit quand même atteindre le resolver — voir
        [[gps-delivery-burkina-faso-2026-08]]."""
        state = make_state(
            current_goal="BUYER_PREORDER_INIT",
            expected_input="CONFIRMATION",
            interpreted_event="UNKNOWN",
            location_shared=True,
        )
        result = run(cognitive_orchestrator(state, None))
        assert result["cognitive_decision"]["next_step"] == "continue_tunnel"
        assert result["cognitive_decision"]["reason"] == "active_goal"

    @pytest.mark.parametrize("event", ["UNKNOWN", "OUT_OF_SCOPE"])
    def test_unknown_intent_without_a_tunnel_clarifies(self, event):
        state = make_state(interpreted_event=event, detected_intent="UNKNOWN", current_goal=None)
        result = run(cognitive_orchestrator(state, None))
        assert result["cognitive_decision"]["phase"] == "reason"
        assert result["cognitive_decision"]["next_step"] == "clarify"
        assert result["cognitive_decision"]["reason"] == "unknown_intent"

    def test_nominal_case_continues(self):
        state = make_state(interpreted_event="NEW_TASK", detected_intent="SALES_PUBLISH_PRODUCT", current_goal=None)
        result = run(cognitive_orchestrator(state, None))
        assert result["cognitive_decision"]["next_step"] == "continue"
        assert result["cognitive_decision"]["reason"] == "nominal"
        assert result["should_replan"] is False
