"""`orchestrator/orchestrator.py::Orchestrator._run_market` — indice d'ÉTAT
de rôle attendu (2026-09-13, incident WhatsApp #3, utilisateur double-rôle).

## Le bug fermé

`ws.workspace_type` est UNIQUE et sticky par numéro de téléphone (même
workspace pour le buyer et le producteur d'un même utilisateur). Un
producteur qui reçoit "🛒 Nouvelle commande — confirmation requise" et
répond "confirmer" alors que son workspace est resté en mode BUYER (une
session buyer antérieure jamais nettoyée, ex. panier abandonné) voyait ce
message routé vers le graphe BUYER — dont le catalogue d'intentions ne
contient même pas PRODUCER_CONFIRM_ORDER/PRODUCER_CANCEL_ORDER. Aucun
réglage du micro-prompt NEW_TASK ne peut compenser un mauvais catalogue de
départ : le LLM ne peut choisir une intention hors du catalogue fourni.

`services/database/buyer.py::confirm_preorder_draft` pose désormais un
indice Redis (`pending_role_hint:{phone}` = "PRODUCER", posé au moment où
CETTE notification producteur est mise en file) que `_run_market` lit et
consomme AVANT de faire confiance à `ws.workspace_type`/`ws.metadata` —
tous deux potentiellement périmés (dernier tour BUYER de ce même numéro).
Un indice d'ÉTAT, jamais un mot-clé du texte reçu — même principe que
`interpreter/routing.py::_cart_pending_signal` (voir
`test_confirmation_free_text_reliability.py`)."""
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from ladini.orchestrator.orchestrator import Orchestrator
from ladini.workspace.models import Workspace


class _FakeRuntime:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def bind_user(self, phone: str) -> None:
        pass

    async def call_db(self, tool_name: str, **kwargs: Any) -> Dict[str, Any]:
        return {"status": "SUCCESS", "data": {"role": "BUYER"}}


class _FakeGraph:
    def __init__(self):
        self.invoked_with_role: str | None = None

    async def ainvoke(self, inputs: Dict[str, Any], config: Any) -> Dict[str, Any]:
        self.invoked_with_role = inputs.get("user_role")
        return {"status": "COMPLETED", "final_response": "ok"}


class _FakeGraphFactory:
    def __init__(self):
        self.graph = _FakeGraph()

    def get_graph(self, role: str, **kwargs: Any) -> _FakeGraph:
        self.graph.requested_role = role
        return self.graph


def _orchestrator(graph_factory: _FakeGraphFactory) -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch._graph_factory = graph_factory
    orch._checkpointer = None
    return orch


def _stale_buyer_workspace() -> Workspace:
    return Workspace(
        workspace_id="+22670000001",
        workspace_type="buyer",
        metadata={"user_role": "BUYER"},
    )


class TestPendingRoleHintOverridesTheStaleWorkspaceType:
    @pytest.mark.asyncio
    async def test_a_producer_role_hint_forces_the_producer_graph(
        self, monkeypatch
    ):
        graph_factory = _FakeGraphFactory()
        orch = _orchestrator(graph_factory)

        monkeypatch.setattr(
            "ladini.orchestrator.orchestrator.build_runtime",
            lambda: _FakeRuntime(),
        )
        monkeypatch.setattr(
            "ladini.orchestrator.orchestrator._get_role_hint",
            lambda key: "PRODUCER",
        )
        released: list[str] = []
        monkeypatch.setattr(
            "ladini.orchestrator.orchestrator._release_role_hint",
            lambda key: released.append(key),
        )

        ws = _stale_buyer_workspace()
        result = await orch._run_market(ws, "confirmer", "+22670000001")

        assert graph_factory.graph.requested_role == "PRODUCER"
        assert result["status"] == "COMPLETED"
        assert released == ["pending_role_hint:+22670000001"]

    @pytest.mark.asyncio
    async def test_the_hint_is_ignored_when_force_role_is_set(self, monkeypatch):
        """`force_role` (relance explicite d'un rôle précis, ex. bascule
        manuelle) reste prioritaire — l'indice ne doit jamais le contredire."""
        graph_factory = _FakeGraphFactory()
        orch = _orchestrator(graph_factory)

        monkeypatch.setattr(
            "ladini.orchestrator.orchestrator.build_runtime",
            lambda: _FakeRuntime(),
        )
        hint_calls: list[str] = []

        def _tracking_get_hint(key: str):
            hint_calls.append(key)
            return "PRODUCER"

        monkeypatch.setattr(
            "ladini.orchestrator.orchestrator._get_role_hint", _tracking_get_hint
        )
        monkeypatch.setattr(
            "ladini.orchestrator.orchestrator._release_role_hint", lambda key: None
        )

        ws = _stale_buyer_workspace()
        await orch._run_market(ws, "confirmer", "+22670000001", force_role=True)

        assert not hint_calls
        assert graph_factory.graph.requested_role == "BUYER"

    @pytest.mark.asyncio
    async def test_no_hint_falls_back_to_the_sticky_workspace_type(
        self, monkeypatch
    ):
        """Non-régression : sans indice en attente (cas normal, immense
        majorité des tours), le comportement historique — `ws.workspace_type`
        fait foi — reste inchangé."""
        graph_factory = _FakeGraphFactory()
        orch = _orchestrator(graph_factory)

        monkeypatch.setattr(
            "ladini.orchestrator.orchestrator.build_runtime",
            lambda: _FakeRuntime(),
        )
        monkeypatch.setattr(
            "ladini.orchestrator.orchestrator._get_role_hint", lambda key: None
        )
        released: list[str] = []
        monkeypatch.setattr(
            "ladini.orchestrator.orchestrator._release_role_hint",
            lambda key: released.append(key),
        )

        ws = _stale_buyer_workspace()
        await orch._run_market(ws, "mes commandes", "+22670000001")

        assert graph_factory.graph.requested_role == "BUYER"
        assert released == []  # rien à relâcher, l'indice n'existait pas

    @pytest.mark.asyncio
    async def test_a_producer_hint_is_not_overridden_by_the_stale_metadata_role(
        self, monkeypatch
    ):
        """Le piège exact de l'incident : `ws.metadata["user_role"]` porte
        encore "BUYER" (dernier tour buyer de ce numéro) — sans garde
        explicite, cette 2e heuristique écraserait l'indice aussitôt posé."""
        graph_factory = _FakeGraphFactory()
        orch = _orchestrator(graph_factory)

        monkeypatch.setattr(
            "ladini.orchestrator.orchestrator.build_runtime",
            lambda: _FakeRuntime(),
        )
        monkeypatch.setattr(
            "ladini.orchestrator.orchestrator._get_role_hint",
            lambda key: "PRODUCER",
        )
        monkeypatch.setattr(
            "ladini.orchestrator.orchestrator._release_role_hint", lambda key: None
        )

        ws = Workspace(
            workspace_id="+22670000001",
            workspace_type="buyer",
            metadata={"user_role": "BUYER", "transaction_payload": {"role": "BUYER"}},
        )
        await orch._run_market(ws, "confirmer", "+22670000001")

        assert graph_factory.graph.requested_role == "PRODUCER"
