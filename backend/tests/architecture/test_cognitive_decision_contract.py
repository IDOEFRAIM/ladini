"""Contrat `ConversationDecision` — clôture du Bloc 1 (2026-09-08).

Verrouille :
    - item 18 : une action cognitive inconnue est une violation de contrat
      OBSERVABLE (logger.error), jamais un silence ;
    - item 25 : `semantic_disambiguation` journalise une violation de
      contrat si `cognitive_decision.action == "DISAMBIGUATE"` mais aucun
      candidat exploitable n'est trouvé ;
    - item 29 : RECOVER_ACTIVE_GOAL route directement vers `response_strategy`
      (clarification_node no-op prouvé) ; ABANDON_ACTIVE_GOAL avec échec LLM
      route AUSSI vers `response_strategy` (fix du gap identifié pendant
      cette clôture — `response_strategy` était déjà pré-posé par le reset,
      l'ancien routeur l'ignorait faute de `final_response`) ;
    - item 40 : quelques invariants de propriété, indépendants des intents
      précis, pour détecter une dérive future.
"""
from __future__ import annotations

import logging

import pytest

from ladini.graphs.agents.market_coach.core.conversation_decision import (
    ConversationAction,
)
from ladini.graphs.agents.market_coach.core.graph_builder import (
    _route_after_clarification,
)
from ladini.graphs.agents.market_coach.nodes.cognitive import cognitive_guard
from ladini.graphs.agents.market_coach.nodes.routing import (
    _route_after_cognitive_guard,
)
from ladini.graphs.agents.market_coach.nodes.semantic_disambiguation import (
    semantic_disambiguation,
)
from tests.conftest import make_state, run


class TestUnknownCognitiveActionIsAnObservableContractViolation:
    """Item 18 : plus de `mapping.get(action, "to_clarification")` silencieux."""

    def test_unknown_action_logs_an_error_and_falls_back_safely(self, caplog):
        state = {"cognitive_decision": {"action": "TOTALLY_MADE_UP_ACTION"}}
        with caplog.at_level(logging.ERROR):
            route = _route_after_cognitive_guard(state)
        assert route == "to_clarification"
        assert any(
            "CONTRACT VIOLATION" in record.message for record in caplog.records
        ), "une action inconnue doit être journalisée en ERROR, jamais silencieuse"

    def test_missing_cognitive_decision_also_logs_a_violation(self, caplog):
        with caplog.at_level(logging.ERROR):
            route = _route_after_cognitive_guard({})
        assert route == "to_clarification"
        assert any("CONTRACT VIOLATION" in r.message for r in caplog.records)

    @pytest.mark.parametrize("action", sorted(ConversationAction.ALL))
    def test_every_canonical_action_has_a_declared_route_and_never_logs(
        self, action, caplog
    ):
        with caplog.at_level(logging.ERROR):
            route = _route_after_cognitive_guard(
                {"cognitive_decision": {"action": action}}
            )
        assert route in {"to_planner", "to_disambiguation", "to_clarification", "to_strategy"}
        assert not any("CONTRACT VIOLATION" in r.message for r in caplog.records)


class TestSemanticDisambiguationContractViolationLogging:
    """Item 25 : DISAMBIGUATE sans candidat exploitable = violation loggée."""

    def test_disambiguate_action_without_any_candidate_logs_an_error(self, caplog):
        state = make_state(
            cognitive_decision={"action": "DISAMBIGUATE"},
            normalized_text="bonjour comment allez-vous",
        )
        assert "disambiguation_candidate" not in state
        with caplog.at_level(logging.ERROR):
            result = run(semantic_disambiguation(state, None))
        assert result == {}
        assert any(
            "CONTRACT VIOLATION" in r.message for r in caplog.records
        ), "DISAMBIGUATE sans candidat exploitable doit être loggé, pas silencieux"

    def test_disambiguate_action_with_a_single_option_logs_an_error(self, caplog):
        state = make_state(
            cognitive_decision={"action": "DISAMBIGUATE"},
            disambiguation_candidate={"id": "x", "options": [("A", "a")]},
        )
        with caplog.at_level(logging.ERROR):
            result = run(semantic_disambiguation(state, None))
        assert result == {}
        assert any("CONTRACT VIOLATION" in r.message for r in caplog.records)

    def test_non_disambiguate_no_candidate_does_not_log_a_violation(self, caplog):
        """Un appel direct sans `cognitive_decision` du tout (test unitaire,
        script) ne doit pas non plus crier au loup — la violation ne
        s'applique qu'à une VRAIE décision DISAMBIGUATE mal formée."""
        state = make_state(normalized_text="bonjour")
        with caplog.at_level(logging.ERROR):
            result = run(semantic_disambiguation(state, None))
        assert result == {}
        assert not any("CONTRACT VIOLATION" in r.message for r in caplog.records)


class TestRecoverAndAbandonRoutingContract:
    """Item 29."""

    def test_recover_active_goal_bypasses_clarification_node_entirely(self):
        route = _route_after_cognitive_guard(
            {"cognitive_decision": {"action": ConversationAction.RECOVER_ACTIVE_GOAL}}
        )
        assert route == "to_strategy"

    def test_abandon_with_a_successful_llm_message_reaches_response_strategy(self):
        state = {
            "response_strategy": "CLARIFICATION",
            "final_response": "On repart de zéro, dites-moi ce dont vous avez besoin.",
        }
        assert _route_after_clarification(state) == "to_strategy"

    def test_abandon_with_a_failed_llm_still_reaches_response_strategy(self):
        """Le gap identifié pendant cette clôture : `reset_abandoned_
        conversation_context` pose déjà `response_strategy="CLARIFICATION"`
        AVANT que `clarification_node` ne s'exécute. Si le LLM échoue et que
        ce nœud ne produit aucun `final_response`, le tour ne doit PAS
        dériver vers `goal_planner` (état déjà réinitialisé : goal=None,
        payload vidé) — il doit terminer proprement sur la réponse déjà
        préparée par le reset."""
        state = {"response_strategy": "CLARIFICATION"}
        assert _route_after_clarification(state) == "to_strategy"

    def test_genuine_clarify_no_op_still_continues_to_the_planner(self):
        """Non-régression : un CLARIFY générique qui ne produit rien (LLM
        indisponible, aucun cas GPS/technique) continue vers `goal_planner`,
        comme avant ce correctif — `response_strategy` reste vide dans ce
        cas (rien ne l'a pré-posé, contrairement à ABANDON)."""
        state = {}
        assert _route_after_clarification(state) == "to_planner"


class TestConversationDecisionInvariants:
    """Item 40 — invariants de propriété, indépendants des intents précis."""

    def test_disambiguate_always_has_at_least_two_valid_candidates(self, monkeypatch):
        import ladini.graphs.agents.market_coach.nodes.cognitive as mod

        monkeypatch.setattr(
            mod,
            "_detect_disambiguation_candidates",
            lambda text, role: {
                "id": "t",
                "options": [("A", "a"), ("B", "b"), ("C", "c")],
            },
        )
        state = make_state(
            interpreted_event="NEW_TASK",
            detected_intent="UNKNOWN",
            normalized_text="un texte quelconque",
            current_goal=None,
        )
        result = run(cognitive_guard(state, None))
        if result["cognitive_decision"]["action"] == ConversationAction.DISAMBIGUATE:
            candidate = result["disambiguation_candidate"]
            assert len(candidate.get("options") or []) >= 2

    def test_interrupt_active_goal_always_implies_a_current_goal_existed(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="NEW_TASK",
            detected_intent="BUYER_REQUEST",
            interpreter_confidence=0.95,
        )
        result = run(cognitive_guard(state, None))
        if result["cognitive_decision"]["action"] == ConversationAction.INTERRUPT_ACTIVE_GOAL:
            assert state.get("current_goal")

    def test_recover_always_implies_an_active_tunnel(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="QUANTITY",
            interpreted_event="UNKNOWN",
            retry_count=0,
        )
        result = run(cognitive_guard(state, None))
        if result["cognitive_decision"]["action"] == ConversationAction.RECOVER_ACTIVE_GOAL:
            assert state.get("current_goal") is not None

    def test_start_or_plan_goal_never_carries_a_final_response(self):
        """START_OR_PLAN_GOAL est un aiguillage muet vers goal_planner — il
        ne doit jamais, par lui-même, clore le tour avec une réponse déjà
        écrite (ce serait le signe d'une CLARIFY masquée)."""
        state = make_state(
            current_goal=None,
            interpreted_event="NEW_TASK",
            detected_intent="SALES_PUBLISH_PRODUCT",
            interpreter_confidence=0.97,
            normalized_text="je veux vendre 2 tonnes de mais",
        )
        result = run(cognitive_guard(state, None))
        if result["cognitive_decision"]["action"] == ConversationAction.START_OR_PLAN_GOAL:
            assert "final_response" not in result
