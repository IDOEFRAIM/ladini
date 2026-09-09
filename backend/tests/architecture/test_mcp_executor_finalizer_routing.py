"""P2-3 (audit architectural 2026-09-08) — Test K du mandat.

Le chaînage no-op `mcp_tool_executor -> procurement_finalizer ->
sales_finalizer -> response_strategy` est remplacé par un VRAI branchement
(`_route_after_mcp_executor`, nodes/routing.py) sur la présence du draft
transactionnel propre à chaque domaine. Ce fichier prouve :

1. le routeur lui-même sélectionne la bonne branche (unitaire) ;
2. le graphe COMPILÉ emprunte réellement cette branche unique — un tour
   PROCUREMENT ne traverse jamais `sales_execution_finalizer` et
   inversement, un tour sans draft transactionnel saute les deux.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest
from langgraph.graph import END, StateGraph

from agriconnect.graphs.agents.market_coach.core.state import MarketAgentState
from agriconnect.graphs.agents.market_coach.nodes.routing import (
    _route_after_mcp_executor,
)
from tests.conftest import make_state


class TestRouteAfterMcpExecutorDiscriminant:
    def test_procurement_draft_present_routes_to_procurement_finalizer(self):
        state = make_state(procurement_draft={"draft_id": "d1", "version": 1})
        assert _route_after_mcp_executor(state) == "to_procurement_finalizer"

    def test_sales_publish_draft_present_routes_to_sales_finalizer(self):
        state = make_state(sales_publish_draft={"draft_id": "d2", "version": 1})
        assert _route_after_mcp_executor(state) == "to_sales_finalizer"

    def test_neither_draft_present_routes_straight_to_response(self):
        state = make_state()
        assert _route_after_mcp_executor(state) == "to_response"

    def test_procurement_draft_takes_priority_if_both_somehow_present(self):
        # Cas normalement impossible (goals disjoints) — le routeur doit
        # rester déterministe plutôt que planter.
        state = make_state(
            procurement_draft={"draft_id": "d1", "version": 1},
            sales_publish_draft={"draft_id": "d2", "version": 1},
        )
        assert _route_after_mcp_executor(state) == "to_procurement_finalizer"


def _build_probe_graph():
    """Reproduit la topologie réelle post-P2-3 avec des nœuds sentinelles,
    pour observer QUELS nœuds sont réellement traversés (et lesquels sont
    court-circuités) — sans dépendre des DB/MCP réels des vrais finalizers."""
    visited: list[str] = []

    async def mcp_tool_executor(state: Dict[str, Any]) -> Dict[str, Any]:
        visited.append("mcp_tool_executor")
        return {}

    async def procurement_execution_finalizer(state: Dict[str, Any]) -> Dict[str, Any]:
        visited.append("procurement_execution_finalizer")
        return {}

    async def sales_execution_finalizer(state: Dict[str, Any]) -> Dict[str, Any]:
        visited.append("sales_execution_finalizer")
        return {}

    async def response_strategy(state: Dict[str, Any]) -> Dict[str, Any]:
        visited.append("response_strategy")
        return {}

    workflow = StateGraph(MarketAgentState)
    workflow.add_node("mcp_tool_executor", mcp_tool_executor)
    workflow.add_node("procurement_execution_finalizer", procurement_execution_finalizer)
    workflow.add_node("sales_execution_finalizer", sales_execution_finalizer)
    workflow.add_node("response_strategy", response_strategy)
    workflow.set_entry_point("mcp_tool_executor")
    workflow.add_conditional_edges(
        "mcp_tool_executor",
        _route_after_mcp_executor,
        {
            "to_procurement_finalizer": "procurement_execution_finalizer",
            "to_sales_finalizer": "sales_execution_finalizer",
            "to_response": "response_strategy",
        },
    )
    workflow.add_edge("procurement_execution_finalizer", "response_strategy")
    workflow.add_edge("sales_execution_finalizer", "response_strategy")
    workflow.add_edge("response_strategy", END)
    return workflow.compile(), visited


class TestCompiledGraphExercisesExactlyOneFinalizer:
    @pytest.mark.asyncio
    async def test_procurement_turn_never_touches_sales_finalizer(self):
        graph, visited = _build_probe_graph()
        await graph.ainvoke(
            make_state(procurement_draft={"draft_id": "d1", "version": 1})
        )
        assert visited == [
            "mcp_tool_executor",
            "procurement_execution_finalizer",
            "response_strategy",
        ]

    @pytest.mark.asyncio
    async def test_sales_turn_never_touches_procurement_finalizer(self):
        graph, visited = _build_probe_graph()
        await graph.ainvoke(
            make_state(sales_publish_draft={"draft_id": "d2", "version": 1})
        )
        assert visited == [
            "mcp_tool_executor",
            "sales_execution_finalizer",
            "response_strategy",
        ]

    @pytest.mark.asyncio
    async def test_other_goal_skips_both_finalizers(self):
        graph, visited = _build_probe_graph()
        await graph.ainvoke(make_state())
        assert visited == ["mcp_tool_executor", "response_strategy"]
