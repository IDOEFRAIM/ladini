"""Phase 7 (mandat 2026-09-08) — scénario de bout en bout via le VRAI graphe
compilé (`StateGraph(...).compile()` + `ainvoke()`, jamais un appel de
fonction direct — mandat §19/§26).

Portée volontairement bornée (voir rapport final, section « risques
restants ») : ce fichier démarre APRÈS la confirmation utilisateur (draft
déjà `EXECUTING`, exactement l'état produit par
`confirmation_gate`/`resolve_sales_confirmation`/`resolve_procurement_
confirmation` — déjà testés bout en bout ailleurs, ex.
`test_sales_publish_draft_anti_regression.py`), et exerce le segment
`mcp_tool_executor -> _route_after_mcp_executor -> <finalizer> ->
response_strategy` — précisément le segment modifié par P0-1 (survie du
canal `sales_publish_draft`) ET P2-3 (sélection réelle du finalizer) — à
travers une VRAIE compilation LangGraph, pas des appels de fonction chaînés
à la main. Démarrer depuis le texte utilisateur brut nécessiterait de
simuler fidèlement le LLM Gateway (interpréteur + planner + validator) —
hors de portée raisonnable de cette passe ; le segment interprétation ->
confirmation est déjà couvert par la suite `interpreter/`/`architecture/
test_*_draft_*` existante.

Deux scénarios (PROCUREMENT / SALES) prouvent, sur le VRAI graphe :
  * le draft transactionnel du bon domaine survit la traversée du graphe
    compilé (P0-1, généralisé au-delà de `sales_publish_draft` seul) ;
  * SEUL le finalizer du domaine concerné s'exécute (P2-3, Test K du
    mandat) — vérifié en observant qu'AUCUNE trace du domaine non concerné
    n'apparaît dans le patch final."""
from __future__ import annotations

from functools import partial

import pytest
from langgraph.graph import END, StateGraph

from agriconnect.graphs.agents.market_coach.core.confirmation_target import (
    ConfirmationTarget,
)
from agriconnect.graphs.agents.market_coach.core.state import MarketAgentState
from agriconnect.graphs.agents.market_coach.domain.procurement_draft import (
    ConfirmProcurementDraft,
    ProcurementDraft,
    ProcurementDraftStatus,
    apply_domain_action as apply_procurement_action,
)
from agriconnect.graphs.agents.market_coach.domain.sales_publish_draft import (
    ConfirmSalesPublishDraft,
    SalesPublishDraft,
    SalesPublishDraftStatus,
    apply_domain_action as apply_sales_action,
)
from agriconnect.graphs.agents.market_coach.flows.buyer.procurement_execution_finalizer import (
    finalize_procurement_execution,
)
from agriconnect.graphs.agents.market_coach.flows.producer.sales_execution_finalizer import (
    finalize_sales_publish_execution,
)
from agriconnect.graphs.agents.market_coach.nodes.executor import mcp_tool_executor
from agriconnect.graphs.agents.market_coach.nodes.routing import (
    _route_after_mcp_executor,
)
from tests.conftest import StubRuntime, run

_ALWAYS_CLAIM = lambda key: True  # noqa: E731


def _executing_procurement_draft() -> ProcurementDraft:
    v1 = ProcurementDraft.new(
        draft_id="e2e-proc-1", product="tomates", quantity=2000.0,
        unit="KG", price=250.0, price_unit="KG",
    )
    target = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
    return apply_procurement_action(
        v1, ConfirmProcurementDraft(target=target), claim=_ALWAYS_CLAIM
    ).draft


def _executing_sales_draft() -> SalesPublishDraft:
    v1 = SalesPublishDraft.new(
        draft_id="e2e-sales-1", product="Maïs blanc", quantity=100.0,
        unit="KG", price=250.0,
    )
    target = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
    return apply_sales_action(
        v1, ConfirmSalesPublishDraft(target=target), claim=_ALWAYS_CLAIM
    ).draft


def _build_real_compiled_segment(mc_runtime):
    """Reproduit EXACTEMENT la topologie réelle post-P2-3
    (core/graph_builder.py) pour ce segment, avec les VRAIS nœuds
    (`mcp_tool_executor`, les deux finalizers) — seul `response_strategy`
    est un sentinel (son propre rendu est hors de portée de ce test).

    `mc_runtime` est lié par `functools.partial`, exactement comme
    `core/graph_builder.py::build_graph` le fait pour chaque nœud réel
    (`partial(_safe_node(fn, name), mc_runtime=mc_runtime)`)."""

    async def response_strategy(state, mc_runtime):
        return {"response_strategy": "REACHED_RESPONSE_STRATEGY"}

    workflow = StateGraph(MarketAgentState)
    workflow.add_node("mcp_tool_executor", partial(mcp_tool_executor, mc_runtime=mc_runtime))
    workflow.add_node(
        "procurement_execution_finalizer",
        partial(finalize_procurement_execution, mc_runtime=mc_runtime),
    )
    workflow.add_node(
        "sales_execution_finalizer",
        partial(finalize_sales_publish_execution, mc_runtime=mc_runtime),
    )
    workflow.add_node("response_strategy", partial(response_strategy, mc_runtime=mc_runtime))
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
    return workflow.compile()


class TestProcurementExecutionSurvivesTheRealCompiledGraph:
    @pytest.mark.asyncio
    async def test_procurement_draft_reaches_executed_via_the_procurement_finalizer_only(self):
        executing = _executing_procurement_draft()
        rt = StubRuntime(
            responses={"create_auction": {"status": "success", "auction_id": "auc-e2e-1"}}
        )
        state = {
            "user_phone": "+22670000000",
            "user_role": "BUYER",
            "current_goal": "PROCUREMENT_CREATE_REQUEST",
            "execution_authorized": True,
            "procurement_draft": executing.to_dict(),
            "transaction_payload": {
                "product": "tomates", "quantity": 2000.0, "unit": "KG",
                "price": 250.0, "price_unit": "KG",
            },
            "working_memory": {},
        }

        graph = _build_real_compiled_segment(rt)
        result = await graph.ainvoke(state, config={"configurable": {}, "recursion_limit": 25})

        # P0-1 (généralisé) : le draft survit la traversée du graphe COMPILÉ.
        final_draft = ProcurementDraft.from_dict(result["procurement_draft"])
        assert final_draft.status == ProcurementDraftStatus.EXECUTED

        # P2-3 (Test K) : SEUL le finalizer PROCUREMENT a tourné — aucune
        # trace du domaine SALES dans le patch final.
        assert "sales_publish_draft" not in result or result["sales_publish_draft"] is None
        assert result["response_strategy"] == "REACHED_RESPONSE_STRATEGY"


class TestSalesExecutionSurvivesTheRealCompiledGraph:
    @pytest.mark.asyncio
    async def test_sales_draft_reaches_published_via_the_sales_finalizer_only(self):
        executing = _executing_sales_draft()
        rt = StubRuntime(
            responses={
                "create_product": {
                    "status": "success",
                    "data": {"product_id": "prod-e2e-1"},
                }
            }
        )
        state = {
            "user_phone": "+22670000000",
            "user_role": "PRODUCER",
            "current_goal": "SALES_PUBLISH_PRODUCT",
            "execution_authorized": True,
            "sales_publish_draft": executing.to_dict(),
            "transaction_payload": {
                "product": "Maïs blanc", "quantity": 100.0, "unit": "KG",
                "price": 250.0, "farm_id": "farm-e2e-1",
            },
            "working_memory": {},
        }

        graph = _build_real_compiled_segment(rt)
        result = await graph.ainvoke(state, config={"configurable": {}, "recursion_limit": 25})

        # P0-1 : le canal `sales_publish_draft` (au cœur du bug P0-1
        # original) survit bien la traversée du graphe COMPILÉ.
        final_draft = SalesPublishDraft.from_dict(result["sales_publish_draft"])
        assert final_draft.status == SalesPublishDraftStatus.PUBLISHED

        # P2-3 : SEUL le finalizer SALES a tourné.
        assert "procurement_draft" not in result or result["procurement_draft"] is None
        assert result["response_strategy"] == "REACHED_RESPONSE_STRATEGY"
