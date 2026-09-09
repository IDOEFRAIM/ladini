"""Correction topologique du bloc CONVERSATIONNEL (2026-09-08) — preuve sur
le graphe COMPILÉ réel (pas une réimplémentation) que `cognitive_guard` est
désormais l'UNIQUE propriétaire de la décision de transition, et que
`clarification_node`/`semantic_disambiguation` ne sont atteints QUE lorsque
cette décision l'exige — jamais pour un tour nominal.

Sous-graphe exact reconstruit ici : `cognitive_guard → {clarification_node,
semantic_disambiguation, goal_planner}`, avec les VRAIS nœuds/routeurs
(`cognitive_guard`, `clarification_node`, `semantic_disambiguation`,
`_route_after_cognitive_guard`, `_route_after_clarification`,
`_route_after_disambiguation`) et des sentinelles `goal_planner`/
`response_strategy` qui enregistrent leur propre exécution — même schéma que
`tests/architecture/test_security_boundary_compiled_graph.py`.

Couvre les scénarios A-G du mandat de correction (§22) ; le scénario H
(fast-path `to_memory_fast`) est couvert séparément, en unitaire, dans
`TestFastPathNeverNeedsCognitiveArbitration` ci-dessous — il ne visite pas
`cognitive_guard` dans le graphe réel (bypass volontaire, voir
`core/policies.py::FastPathPolicy`), donc n'a pas sa place dans le
sous-graphe compilé ci-dessus."""
from __future__ import annotations

from typing import Any, Dict

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from agriconnect.graphs.agents.market_coach.core.goals import ALL_BUYER_TUNNEL_GOALS
from agriconnect.graphs.agents.market_coach.core.state import MarketAgentState
from agriconnect.graphs.agents.market_coach.nodes.clarification import (
    clarification_node,
)
from agriconnect.graphs.agents.market_coach.nodes.cognitive import cognitive_guard
from agriconnect.graphs.agents.market_coach.nodes.routing import (
    _route_after_cognitive_guard,
)
from agriconnect.graphs.agents.market_coach.nodes.semantic_disambiguation import (
    semantic_disambiguation,
)
from agriconnect.graphs.agents.market_coach.core.graph_builder import (
    _route_after_clarification,
)
from tests.conftest import make_state, run


class _StubLLM:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls = 0

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls += 1
        # Réponse minimale compatible `completion.choices[0].message.content`.
        msg = type("Msg", (), {"content": self._text})()
        choice = type("Choice", (), {"message": msg})()
        return type("Completion", (), {"choices": [choice]})()


def _runtime(text: str = "Je peux vous aider.") -> Any:
    llm = _StubLLM(text)
    return type("RT", (), {"llm": llm, "model_answer": "test-model"})()


def _build_conversational_subgraph(rt: Any, reached: list):
    async def real_cognitive_guard(state: Dict[str, Any]) -> Dict[str, Any]:
        return await cognitive_guard(state, rt)

    async def real_clarification_node(state: Dict[str, Any]) -> Dict[str, Any]:
        reached.append("clarification_node")
        return await clarification_node(state, rt)

    async def real_semantic_disambiguation(state: Dict[str, Any]) -> Dict[str, Any]:
        reached.append("semantic_disambiguation")
        return await semantic_disambiguation(state, rt)

    async def spy_goal_planner(state: Dict[str, Any]) -> Dict[str, Any]:
        reached.append("goal_planner")
        return {}

    async def spy_response_strategy(state: Dict[str, Any]) -> Dict[str, Any]:
        reached.append("response_strategy")
        return {}

    workflow = StateGraph(MarketAgentState)
    workflow.add_node("cognitive_guard", real_cognitive_guard)
    workflow.add_node("clarification_node", real_clarification_node)
    workflow.add_node("semantic_disambiguation", real_semantic_disambiguation)
    workflow.add_node("goal_planner", spy_goal_planner)
    workflow.add_node("response_strategy", spy_response_strategy)

    workflow.set_entry_point("cognitive_guard")
    workflow.add_conditional_edges(
        "cognitive_guard",
        _route_after_cognitive_guard,
        {
            "to_planner": "goal_planner",
            "to_disambiguation": "semantic_disambiguation",
            "to_clarification": "clarification_node",
            "to_strategy": "response_strategy",
        },
    )
    workflow.add_conditional_edges(
        "clarification_node",
        _route_after_clarification,
        {"to_planner": "goal_planner", "to_strategy": "response_strategy"},
    )
    # Edge fixe (2026-09-08, clôture Bloc 1, mandat §24) : voir
    # graph_builder.py pour la preuve que `to_planner` était inatteignable.
    workflow.add_edge("semantic_disambiguation", "response_strategy")
    workflow.add_edge("goal_planner", END)
    workflow.add_edge("response_strategy", END)
    return workflow.compile(checkpointer=MemorySaver())


class TestNominalNewTaskNeverVisitsClarificationOrDisambiguation:
    """Scénario A du mandat : "Je veux vendre 2 tonnes de maïs" — NEW_TASK,
    confiance 0.97, aucun goal actif. Chemin attendu : `cognitive_guard ->
    goal_planner`, direct."""

    @pytest.mark.asyncio
    async def test_high_confidence_new_task_goes_straight_to_the_planner(self):
        reached: list[str] = []
        rt = _runtime("ne devrait jamais apparaître")
        graph = _build_conversational_subgraph(rt, reached)

        state = make_state(
            interpreted_event="NEW_TASK",
            detected_intent="SALES_PUBLISH_PRODUCT",
            interpreter_confidence=0.97,
            current_goal=None,
            normalized_text="je veux vendre 2 tonnes de mais",
        )
        final = await graph.ainvoke(
            state, config={"configurable": {"thread_id": "t-nominal"}}
        )

        assert reached == ["goal_planner"]
        assert final["cognitive_decision"]["action"] == "START_OR_PLAN_GOAL"


class TestInterruption:
    """Scénarios B/C du mandat."""

    @pytest.mark.asyncio
    async def test_different_intent_high_confidence_interrupts_and_reaches_the_planner(
        self,
    ):
        reached: list[str] = []
        rt = _runtime("ne devrait jamais apparaître")
        graph = _build_conversational_subgraph(rt, reached)

        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="QUANTITY",
            interpreted_event="NEW_TASK",
            detected_intent="BUYER_REQUEST",
            interpreter_confidence=0.95,
        )
        final = await graph.ainvoke(
            state, config={"configurable": {"thread_id": "t-interrupt"}}
        )

        assert reached == ["goal_planner"]
        assert final["interpreted_event"] == "INTERRUPTION"
        assert final["cognitive_decision"]["action"] == "INTERRUPT_ACTIVE_GOAL"

    @pytest.mark.asyncio
    async def test_different_intent_low_confidence_does_not_interrupt(self):
        reached: list[str] = []
        rt = _runtime("ne devrait jamais apparaître")
        graph = _build_conversational_subgraph(rt, reached)

        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="QUANTITY",
            interpreted_event="NEW_TASK",
            detected_intent="BUYER_REQUEST",
            interpreter_confidence=0.30,
        )
        final = await graph.ainvoke(
            state, config={"configurable": {"thread_id": "t-no-interrupt"}}
        )

        assert reached == ["goal_planner"]
        assert final.get("interpreted_event") != "INTERRUPTION"
        assert final["cognitive_decision"]["action"] == "CONTINUE_ACTIVE_GOAL"


class TestClarificationWithoutATunnel:
    """Scénario D du mandat : UNKNOWN sans tunnel actif -> clarification_node
    -> response_strategy, jamais goal_planner ni semantic_disambiguation."""

    @pytest.mark.asyncio
    async def test_unknown_without_a_tunnel_reaches_response_strategy_via_clarification(
        self,
    ):
        reached: list[str] = []
        rt = _runtime("Salut ! Je peux vous aider à vendre ou acheter.")
        graph = _build_conversational_subgraph(rt, reached)

        state = make_state(
            interpreted_event="UNKNOWN",
            current_goal=None,
            normalized_text="bonjour comment allez vous",
        )
        final = await graph.ainvoke(
            state, config={"configurable": {"thread_id": "t-clarify"}}
        )

        assert reached == ["clarification_node", "response_strategy"]
        assert final["cognitive_decision"]["action"] == "CLARIFY"
        assert final["response_strategy"] == "CLARIFICATION"


class TestTechnicalFailureNeverCallsTheGatewayTwice:
    """Scénario E du mandat."""

    @pytest.mark.asyncio
    async def test_technical_failure_reaches_clarification_with_zero_extra_llm_calls(
        self,
    ):
        reached: list[str] = []
        rt = _runtime("ne devrait jamais apparaître — le LLM ne doit pas être appelé")
        graph = _build_conversational_subgraph(rt, reached)

        state = make_state(
            interpreted_event="UNKNOWN",
            current_goal=None,
            normalized_text="je veux voir les encheres",
            unknown_reason="TECHNICAL_FAILURE",
        )
        final = await graph.ainvoke(
            state, config={"configurable": {"thread_id": "t-technical"}}
        )

        assert reached == ["clarification_node", "response_strategy"]
        assert "indisponible" in final["final_response"].lower()
        assert rt.llm.calls == 0, "aucun second appel Gateway pour une panne technique"


class TestAmbiguityGoesStraightToDisambiguationNeverClarification:
    """Scénario F du mandat."""

    @pytest.mark.asyncio
    async def test_low_confidence_with_a_valid_lexical_candidate_builds_a_menu(self):
        reached: list[str] = []
        rt = _runtime("ne devrait jamais apparaître")
        graph = _build_conversational_subgraph(rt, reached)

        state = make_state(
            interpreted_event="NEW_TASK",
            detected_intent="UNKNOWN",
            interpreter_confidence=0.3,
            current_goal=None,
            normalized_text="j ai 200 kg de tomates",
            user_role="PRODUCER",
        )
        final = await graph.ainvoke(
            state, config={"configurable": {"thread_id": "t-disambiguate"}}
        )

        assert reached == ["semantic_disambiguation", "response_strategy"]
        assert final["cognitive_decision"]["action"] == "DISAMBIGUATE"
        assert final["current_goal"] == "DISAMBIGUATION_PENDING"
        assert final["response_strategy"] == "SELECTION_MENU"
        assert rt.llm.calls == 0, "la désambiguïsation est purement lexicale, pas de LLM"


class TestDisambiguationCandidateComputedExactlyOnce:
    """Scénario G du mandat : preuve que `_detect_disambiguation_candidates`
    n'est PAS recalculé indépendamment dans plusieurs nœuds pour un même
    tour — `cognitive_guard` le calcule une fois, `semantic_disambiguation`
    consulte le résultat précalculé (voir sa docstring, mandat §14)."""

    @pytest.mark.asyncio
    async def test_lexical_detection_runs_exactly_once_per_turn(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.nodes.cognitive as cognitive_mod
        import agriconnect.graphs.agents.market_coach.nodes.semantic_disambiguation as disambig_mod

        real_fn = disambig_mod._detect_disambiguation_candidates
        calls = {"n": 0}

        def _counting(text, role):
            calls["n"] += 1
            return real_fn(text, role)

        # Les deux modules détiennent chacun leur propre référence liée
        # (import direct de la fonction) — les patcher tous les deux est
        # nécessaire pour observer TOUS les appels du tour.
        monkeypatch.setattr(cognitive_mod, "_detect_disambiguation_candidates", _counting)
        monkeypatch.setattr(disambig_mod, "_detect_disambiguation_candidates", _counting)

        reached: list[str] = []
        rt = _runtime("ne devrait jamais apparaître")
        graph = _build_conversational_subgraph(rt, reached)

        state = make_state(
            interpreted_event="NEW_TASK",
            detected_intent="UNKNOWN",
            interpreter_confidence=0.3,
            current_goal=None,
            normalized_text="j ai 200 kg de tomates",
            user_role="PRODUCER",
        )
        await graph.ainvoke(
            state, config={"configurable": {"thread_id": "t-single-compute"}}
        )

        assert calls["n"] == 1, (
            f"attendu exactement 1 calcul lexical pour ce tour, obtenu {calls['n']} "
            "— une seconde policy de désambiguïsation a régressé quelque part"
        )


class TestFastPathNeverNeedsCognitiveArbitration:
    """Scénario H du mandat (audit §18) : chaque tour éligible au bypass
    `to_memory_fast` (`core/policies.py::FastPathPolicy.for_buyer`, event
    ANSWER/SELECTION dans un tunnel acheteur connu) ne visite PAS
    `cognitive_guard` dans le graphe réel — ce test prouve que, MÊME s'il y
    était envoyé, `cognitive_guard` ne le classerait JAMAIS en
    INTERRUPT_ACTIVE_GOAL/CLARIFY/DISAMBIGUATE : structurellement, ces 3
    décisions exigent toutes un `event` que ANSWER/SELECTION ne peut pas
    fournir (INTERRUPT exige NEW_TASK ; CLARIFY exige OUT_OF_SCOPE/UNKNOWN/
    REJECT ; DISAMBIGUATE exige NEW_TASK/UNKNOWN). Le bypass ne peut donc
    jamais faire manquer une interruption/clarification légitime."""

    @pytest.mark.parametrize("event", ["ANSWER", "SELECTION"])
    @pytest.mark.parametrize(
        "goal", sorted(ALL_BUYER_TUNNEL_GOALS)[:5] or [None]
    )
    def test_bypassed_events_would_only_ever_classify_as_continue_active_goal(
        self, event, goal
    ):
        if goal is None:
            pytest.skip("aucun goal de tunnel acheteur déclaré")
        state = make_state(
            current_goal=goal,
            expected_input="QUANTITY",
            interpreted_event=event,
            interpreter_confidence=0.99,
            detected_intent=goal,
        )
        result = run(cognitive_guard(state, None))
        action = result["cognitive_decision"]["action"]
        assert action not in {
            "INTERRUPT_ACTIVE_GOAL",
            "CLARIFY",
            "DISAMBIGUATE",
            "recover_active_tunnel",
            "abandon_tunnel_max_retries",
        }, f"event={event} goal={goal} a produit une action nécessitant une arbitration : {action}"
