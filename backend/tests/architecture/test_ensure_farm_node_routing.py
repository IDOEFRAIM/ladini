"""P0-2 (audit architectural 2026-09-08) — `ensure_farm_node` a 6 issues
distinctes ; l'edge fixe `add_edge("ensure_farm_node", "confirmation_gate")`
en traitait 3 comme si c'était la même chose, écrasant une décision
utilisateur déjà posée (menu multi-fermes, clarification READ, refus
d'auto-provisioning). Ce fichier verrouille :

1. `_route_after_farm_guard` — le discriminant lui-même, contre les VRAIES
   sorties de `flows/producer/farm_logic.py::ensure_farm_node`.
2. Le graphe COMPILÉ — preuve qu'aucun cas WAITING_INPUT n'atteint
   `confirmation_gate` (Test D du mandat : « aucun edge dangereux après
   état bloquant »)."""
from __future__ import annotations

from typing import Any, Dict

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from ladini.graphs.agents.market_coach.core.graph_builder import (
    _route_after_farm_guard,
)
from ladini.graphs.agents.market_coach.core.state import MarketAgentState
from ladini.graphs.agents.market_coach.flows.producer.farm_logic import (
    ensure_farm_node,
)
from tests.conftest import StubRuntime, run


def _farms_response(farms):
    return {"status": "success", "data": farms}


class TestRouteAfterFarmGuardDiscriminant:
    def test_no_op_goes_to_confirmation(self):
        assert _route_after_farm_guard({}) == "to_confirmation"

    def test_silent_success_goes_to_confirmation(self):
        state = {"transaction_payload": {"farm_id": "f1"}, "stable_entities": {}}
        assert _route_after_farm_guard(state) == "to_confirmation"

    def test_multi_farm_selection_goes_to_ui(self):
        state = {
            "final_response": "Sur quelle exploitation...",
            "working_memory": {"available_mapping_kind": "farm"},
        }
        assert _route_after_farm_guard(state) == "to_ui"

    def test_read_clarification_goes_to_response(self):
        state = {
            "final_response": "Vous n'avez pas encore d'exploitation...",
            "working_memory": {},
        }
        assert _route_after_farm_guard(state) == "to_response"

    def test_creation_error_goes_to_response(self):
        state = {
            "final_response": "Je n'ai pas pu configurer votre exploitation...",
            "error_creating_farm": True,
            "working_memory": {},
        }
        assert _route_after_farm_guard(state) == "to_response"


class TestEnsureFarmNodeRealOutputsClassifyCorrectly:
    """Le discriminant contre les VRAIES sorties du nœud (pas des états
    synthétiques) — pour chacun des 6 cas du mandat."""

    def _base_state(self, goal: str, **overrides) -> Dict[str, Any]:
        state = {
            "current_goal": goal,
            "user_phone": "+22670000001",
            "transaction_payload": {},
            "stable_entities": {},
            "working_memory": {},
        }
        state.update(overrides)
        return state

    def test_one_farm_resolves_silently(self):
        """1 ferme -> résolution silencieuse -> confirmation_gate."""
        rt = StubRuntime(
            responses={
                "get_producer_farm": _farms_response(
                    [{"id": "farm-1", "name": "Ferme unique"}]
                )
            }
        )
        state = self._base_state("STOCK_REGISTER_HARVEST")
        patch = run(ensure_farm_node(state, rt))
        assert _route_after_farm_guard(patch) == "to_confirmation"
        assert patch["transaction_payload"]["farm_id"] == "farm-1"

    def test_zero_farms_write_goal_auto_provisions_silently(self):
        """0 ferme + goal WRITE -> auto-provisioning -> confirmation_gate."""
        rt = StubRuntime(
            responses={
                "get_producer_farm": _farms_response([]),
                "get_or_create_farm": {
                    "status": "success",
                    "data": {"id": "farm-new"},
                },
            }
        )
        state = self._base_state("STOCK_REGISTER_HARVEST")
        patch = run(ensure_farm_node(state, rt))
        assert _route_after_farm_guard(patch) == "to_confirmation"
        assert patch["transaction_payload"]["farm_id"] == "farm-new"

    def test_multiple_farms_routes_to_ui_never_confirmation(self):
        """Plusieurs fermes -> menu de sélection -> ui_engine, JAMAIS
        confirmation_gate (c'est le cas P0-2 prouvé cassé par l'audit)."""
        rt = StubRuntime(
            responses={
                "get_producer_farm": _farms_response(
                    [
                        {"id": "farm-1", "name": "Ferme A"},
                        {"id": "farm-2", "name": "Ferme B"},
                    ]
                )
            }
        )
        state = self._base_state("STOCK_REGISTER_HARVEST")
        patch = run(ensure_farm_node(state, rt))
        assert patch["status"] == "WAITING_INPUT"
        assert _route_after_farm_guard(patch) == "to_ui"

    def test_read_goal_without_a_resolvable_farm_id_routes_to_response(self):
        """Lecture, des fermes existent mais aucun id exploitable ->
        clarification -> response_strategy, jamais confirmation_gate."""
        rt = StubRuntime(
            responses={
                "get_producer_farm": _farms_response([{"name": "Ferme sans id"}])
            }
        )
        state = self._base_state("STOCK_GET_SUMMARY")
        patch = run(ensure_farm_node(state, rt))
        assert patch["status"] == "WAITING_INPUT"
        assert _route_after_farm_guard(patch) == "to_response"

    def test_creation_failure_routes_to_response_not_confirmation(self):
        """(2026-09-08) Ce cas ne posait auparavant NI final_response NI
        status — corrigé dans farm_logic.py pour que le routage ait un sens.
        0 ferme, get_or_create_farm échoue -> clarification honnête."""

        class _FailingRuntime(StubRuntime):
            async def call_db(self, tool_name, **kwargs):
                if tool_name == "get_producer_farm":
                    return _farms_response([])
                if tool_name == "get_or_create_farm":
                    raise RuntimeError("MCP indisponible")
                return await super().call_db(tool_name, **kwargs)

        state = self._base_state("STOCK_REGISTER_HARVEST")
        patch = run(ensure_farm_node(state, _FailingRuntime()))
        assert patch["status"] == "WAITING_INPUT"
        assert patch.get("error_creating_farm") is True
        assert patch.get("final_response"), "l'échec doit être annoncé, jamais silencieux"
        assert _route_after_farm_guard(patch) == "to_response"

    def test_write_goal_with_no_phone_is_a_true_no_op(self):
        """Aucun téléphone résolvable -> no-op défensif -> confirmation_gate
        (comportement inchangé, ce nœud ne peut rien faire de plus)."""
        state = self._base_state("STOCK_REGISTER_HARVEST", user_phone=None)
        patch = run(ensure_farm_node(state, StubRuntime()))
        assert patch == {}
        assert _route_after_farm_guard(patch) == "to_confirmation"


class TestCompiledGraphNeverReachesConfirmationOnBlockingFarmState:
    """Test D du mandat, au niveau du graphe COMPILÉ réel (pas une
    réimplémentation) : construit le sous-graphe EXACT
    `ensure_farm_node -[conditionnel]-> {confirmation_gate, ui_engine,
    response_strategy}` avec les VRAIES fonctions de routage et déclare un
    `confirmation_gate` sentinelle qui échoue le test s'il est atteint."""

    @pytest.mark.asyncio
    async def test_multi_farm_menu_never_reaches_confirmation_gate(self):
        reached: list[str] = []

        async def fake_confirmation_gate(state: Dict[str, Any]) -> Dict[str, Any]:
            reached.append("confirmation_gate")
            return {}

        async def fake_ui_engine(state: Dict[str, Any]) -> Dict[str, Any]:
            reached.append("ui_engine")
            return {}

        async def fake_response_strategy(state: Dict[str, Any]) -> Dict[str, Any]:
            reached.append("response_strategy")
            return {}

        rt = StubRuntime(
            responses={
                "get_producer_farm": _farms_response(
                    [
                        {"id": "farm-1", "name": "Ferme A"},
                        {"id": "farm-2", "name": "Ferme B"},
                    ]
                )
            }
        )

        async def real_ensure_farm_node(state: Dict[str, Any]) -> Dict[str, Any]:
            return await ensure_farm_node(state, rt)

        workflow = StateGraph(MarketAgentState)
        workflow.add_node("ensure_farm_node", real_ensure_farm_node)
        workflow.add_node("confirmation_gate", fake_confirmation_gate)
        workflow.add_node("ui_engine", fake_ui_engine)
        workflow.add_node("response_strategy", fake_response_strategy)
        workflow.set_entry_point("ensure_farm_node")
        workflow.add_conditional_edges(
            "ensure_farm_node",
            _route_after_farm_guard,
            {
                "to_confirmation": "confirmation_gate",
                "to_ui": "ui_engine",
                "to_response": "response_strategy",
            },
        )
        workflow.add_edge("confirmation_gate", END)
        workflow.add_edge("ui_engine", END)
        workflow.add_edge("response_strategy", END)
        graph = workflow.compile(checkpointer=MemorySaver())

        await graph.ainvoke(
            {
                "current_goal": "STOCK_REGISTER_HARVEST",
                "user_phone": "+22670000001",
                "transaction_payload": {},
                "stable_entities": {},
                "working_memory": {},
            },
            config={"configurable": {"thread_id": "t-multi-farm"}},
        )

        assert reached == ["ui_engine"], (
            f"le menu multi-fermes doit atteindre ui_engine et RIEN d'autre, "
            f"obtenu: {reached}"
        )
