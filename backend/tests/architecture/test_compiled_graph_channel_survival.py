"""P0-1 (audit architectural 2026-09-08) — preuve par le GRAPHE COMPILÉ,
jamais par appel direct de nœud.

Root cause de l'incident : LangGraph filtre silencieusement, à la
traversée du graphe COMPILÉ, toute clé absente du schéma d'état
(`MarketAgentState`) — sans exception, sans avertissement. `dict.update(patch)`
(utilisé par la quasi-totalité des tests existants et par
`graph_builder.py::_run_producer_write_flow`) ne reproduit PAS ce filtrage :
une clé non déclarée y survit sans problème. C'est exactement pourquoi
`sales_publish_draft` — écrit et lu à 3 sites réels
(`confirmation_gate.py`, `sales_confirmation.py`,
`sales_execution_finalizer.py`) — a pu rester non déclaré sans qu'aucun test
ne le détecte : aucun ne traversait le graphe compilé pour ce champ.

Ce fichier construit un `StateGraph(MarketAgentState)` MINIMAL (2 nœuds
passthrough triviaux, sans dépendance MCP/LLM) qui utilise le VRAI schéma
`MarketAgentState` du projet — le mécanisme testé est le filtrage de
LangGraph lui-même, pas la logique métier des nœuds (déjà couverte par les
tests unitaires existants). C'est la même méthode de preuve que l'audit
lui-même a utilisée pour découvrir le bug.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from ladini.graphs.agents.market_coach.core.state import MarketAgentState


def _build_probe_graph(field: str, value: Any):
    """Graphe à 2 nœuds : `writer` pose UN champ donné (fermeture — jamais
    lu depuis l'état d'entrée, qui est LUI-MÊME filtré par le schéma avant
    que le premier nœud ne s'exécute), `reader` fait un tour de plus pour
    prouver la survie via checkpoint. Utilise le VRAI `MarketAgentState`
    comme schéma — c'est CE schéma qui décide, à la compilation, quelles
    clés survivent."""

    async def writer(state: Dict[str, Any]) -> Dict[str, Any]:
        return {field: value}

    async def reader(state: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    workflow = StateGraph(MarketAgentState)
    workflow.add_node("writer", writer)
    workflow.add_node("reader", reader)
    workflow.set_entry_point("writer")
    workflow.add_edge("writer", "reader")
    workflow.add_edge("reader", END)
    return workflow.compile(checkpointer=MemorySaver())


@pytest.mark.asyncio
class TestDeclaredChannelsSurviveTheCompiledGraph:
    async def test_sales_publish_draft_survives_a_real_compiled_turn(self):
        """LE test qui aurait attrapé P0-1 : `sales_publish_draft` doit
        survivre à la traversée du graphe compilé, exactement comme
        `procurement_draft`/`preorder_draft`."""
        draft_v1 = {"draft_id": "SP1", "version": 1, "status": "PENDING"}
        graph = _build_probe_graph("sales_publish_draft", draft_v1)
        config = {"configurable": {"thread_id": "t-sales-draft"}}

        result = await graph.ainvoke({}, config=config)

        assert result.get("sales_publish_draft") == draft_v1, (
            "sales_publish_draft a été supprimé par le graphe compilé — "
            "P0-1 non corrigé (champ non déclaré dans MarketAgentState)"
        )

        # Tour N+1 : le checkpoint doit RESTITUER la même valeur sans qu'on
        # la repasse en entrée — c'est le contrat DURABLE de state_profile.py.
        result_n1 = await graph.ainvoke({}, config=config)
        assert result_n1.get("sales_publish_draft") == draft_v1, (
            "sales_publish_draft n'a pas survécu au tour suivant via le "
            "checkpoint — contrat DURABLE rompu"
        )

    @pytest.mark.parametrize(
        "field,value",
        [
            ("procurement_draft", {"draft_id": "P1", "version": 1}),
            ("preorder_draft", {"draft_id": "PR1", "version": 1}),
            ("sales_publish_draft", {"draft_id": "S1", "version": 1}),
        ],
    )
    async def test_the_three_transactional_drafts_are_treated_identically(
        self, field, value
    ):
        """Non-régression : les 3 drafts transactionnels (PROCUREMENT,
        PREORDER, SALES) doivent avoir un contrat IDENTIQUE — c'est
        précisément l'absence de symétrie qui a produit P0-1."""
        graph = _build_probe_graph(field, value)
        config = {"configurable": {"thread_id": f"t-{field}"}}

        result = await graph.ainvoke({}, config=config)
        assert result.get(field) == value, f"{field} a été supprimé par le graphe compilé"

    async def test_an_undeclared_field_really_would_be_dropped(self):
        """Test témoin — prouve que le mécanisme de preuve lui-même est
        valide (le graphe compilé filtre vraiment, ce n'est pas un faux
        négatif de la méthode)."""
        graph = _build_probe_graph(
            "this_field_does_not_exist_in_market_agent_state", {"x": 1}
        )
        config = {"configurable": {"thread_id": "t-undeclared"}}

        result = await graph.ainvoke({}, config=config) or {}
        assert "this_field_does_not_exist_in_market_agent_state" not in result


class TestThreeRegistriesAgree:
    """Test B du mandat — pour chaque champ transactionnel critique,
    `core/state.py` (déclaration + reducer) et `core/state_profile.py`
    (lifecycle) doivent être cohérents. Un champ dans l'un sans l'autre est
    exactement la classe de bug qui a produit P0-1."""

    @pytest.mark.parametrize(
        "field",
        ["procurement_draft", "preorder_draft", "sales_publish_draft"],
    )
    def test_each_transactional_draft_is_declared_and_durable(self, field):
        import typing

        from ladini.graphs.agents.market_coach.core.state_profile import (
            FieldLifecycle,
            get_field_spec,
        )

        hints = typing.get_type_hints(MarketAgentState, include_extras=True)
        assert field in hints, f"{field} absent de MarketAgentState (core/state.py)"

        spec = get_field_spec(field)
        assert spec is not None, f"{field} absent de core/state_profile.py"
        assert spec.lifecycle == FieldLifecycle.DURABLE, (
            f"{field} doit être DURABLE — un draft en cours de confirmation "
            f"doit survivre au tour suivant"
        )
