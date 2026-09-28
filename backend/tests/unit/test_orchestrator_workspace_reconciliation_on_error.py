"""`orchestrator/orchestrator.py::Orchestrator.handle` — `ws.active_goal` périmé
peut ressusciter un but invalide après un échec technique mi-tour.

## Le mécanisme exact

`_sync_workspace` est la SEULE fonction qui dérive `ws.active_goal`/
`ws.active_form` du résultat RÉEL d'un tour (`final.get("current_goal")`).
Elle n'est appelée QUE sur le chemin succès (`else:`, ligne ~278). Les 3
branches d'échec (`AgentCircuitBreaker`, `asyncio.TimeoutError`, `Exception`
générique) appellent directement `_flush_workspace` SANS jamais passer par
`_sync_workspace` — `ws.active_goal` reste donc à sa valeur D'AVANT ce tour.

Pendant ce temps, chaque nœud du graphe qui a RÉELLEMENT terminé avant
l'échec a déjà mis à jour le checkpoint LangGraph via `workspace/
checkpointer.py::_persist_state` (`workspace.agent_state = state;
workspace.mark_dirty()`, appelé après CHAQUE nœud). Le checkpoint reflète
donc correctement la progression réelle (ex: `current_goal` déjà remis à
`None` par un nœud qui a fini AVANT que le nœud SUIVANT ne plante) —
seul `ws.active_goal`, un champ SÉPARÉ, reste périmé.

Au tour SUIVANT, `_run_market` réinjecte explicitement `"current_goal":
ws.active_goal or None` comme INPUT du graphe (`MarketAgentState.current_goal`
est un canal `replace_value` — cet input écrase donc ce que le checkpoint,
pourtant correct, contenait déjà) : le but périmé ressuscite.

## Méthode de test

Mocks minimaux sur les 3 collaborateurs de `Orchestrator.handle` (`resolver`,
`_checkpointer`, `_run_market`) plutôt qu'un graphe réel complet — même
esprit que `test_orchestrator_dual_role_hint.py`, qui teste déjà `_run_market`
en isolation. `_run_market` est remplacé par un double qui simule EXACTEMENT
ce qu'un tour réel partiellement réussi ferait : marquer le workspace dirty
(comme le ferait `_persist_state` après un nœud réel) puis lever l'exception
— sans quoi `_flush_workspace` sauterait le save (`if not ws.is_dirty: skip`)
et le test ne prouverait rien. Le faux `_checkpointer.aget_tuple` renvoie le
checkpoint "vérité" (`current_goal=None`, le but a été correctement nettoyé
par le dernier nœud qui a fini avant le crash) que la réconciliation doit
aller relire."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import pytest

from ladini.orchestrator.orchestrator import Orchestrator
from ladini.workspace.models import Workspace


class _FakeCheckpointTuple:
    def __init__(self, channel_values: Dict[str, Any]) -> None:
        self.checkpoint = {"channel_values": channel_values}


class _FakeCheckpointer:
    """`aget_tuple` renvoie la vérité checkpoint courante — indépendante de
    `ws.active_goal` — pour les deux appelants réels : `_load_before_state`
    (début de tour, best-effort) et la réconciliation post-échec (le
    mécanisme testé ici)."""

    def __init__(self, channel_values: Dict[str, Any]) -> None:
        self._channel_values = channel_values
        self.aget_tuple_calls = 0

    def attach_workspace(self, *_a: Any, **_kw: Any) -> None:
        pass

    def detach_workspace(self, *_a: Any, **_kw: Any) -> None:
        pass

    async def aget_tuple(self, _config: Any) -> _FakeCheckpointTuple:
        self.aget_tuple_calls += 1
        return _FakeCheckpointTuple(dict(self._channel_values))

    def finalize_for_persistence(self, _ws: Workspace) -> Dict[str, Any]:
        return {}


class _FakeStore:
    def __init__(self, ws: Workspace) -> None:
        self._ws = ws
        self.saved_snapshots: list[Workspace] = []

    async def get(self, _workspace_id: str) -> Workspace:
        return self._ws

    async def save(self, workspace: Workspace) -> bool:
        # Copie superficielle des champs qui nous intéressent — le VRAI
        # `save()` sérialise en JSON, mais le point testé ici est la valeur
        # de `active_goal` au moment du flush, pas l'encodage.
        snap = Workspace(
            workspace_id=workspace.workspace_id,
            workspace_type=workspace.workspace_type,
            active_goal=workspace.active_goal,
            active_form=workspace.active_form,
            metadata=dict(workspace.metadata or {}),
        )
        self.saved_snapshots.append(snap)
        return True


class _FakeResolver:
    def __init__(self, store: _FakeStore) -> None:
        self.store = store

    async def resolve(self, workspace_id: str, _workspace_type: Optional[str]) -> Workspace:
        return await self.store.get(workspace_id)


def _stale_workspace(active_goal: str) -> Workspace:
    return Workspace(
        workspace_id="+22670000001",
        workspace_type="producer",
        active_goal=active_goal,
        metadata={"user_role": "PRODUCER"},
    )


def _orchestrator(ws: Workspace, checkpointer: _FakeCheckpointer) -> tuple[Orchestrator, _FakeStore]:
    store = _FakeStore(ws)
    orch = object.__new__(Orchestrator)
    orch.resolver = _FakeResolver(store)
    orch._checkpointer = checkpointer
    orch._graph_factory = None
    return orch, store


async def _crashing_run_market_timeout(ws: Workspace, *_a: Any, **_kw: Any) -> Dict[str, Any]:
    """Simule un tour où au moins un nœud a RÉELLEMENT terminé (et donc
    marqué le workspace dirty via `_persist_state`, comme en production)
    avant qu'une opération plus loin dans le tour ne dépasse le budget
    temps — `asyncio.TimeoutError` est exactement ce que `handle()` attrape
    quand `asyncio.wait_for(self._run_market(...), timeout=...)` expire."""
    ws.mark_dirty()
    raise asyncio.TimeoutError("simulated slow MCP call")


async def _crashing_run_market_exception(ws: Workspace, *_a: Any, **_kw: Any) -> Dict[str, Any]:
    ws.mark_dirty()
    raise RuntimeError("simulated unexpected node bug")


class TestStaleActiveGoalIsReconciledOnTimeout:
    @pytest.mark.asyncio
    async def test_active_goal_is_reconciled_from_the_real_checkpoint_not_left_stale(
        self, monkeypatch
    ):
        ws = _stale_workspace("SALES_PUBLISH_PRODUCT")
        # Vérité checkpoint : un nœud a DÉJÀ remis current_goal à None avant
        # que le tour ne parte en timeout plus loin dans le graphe.
        checkpointer = _FakeCheckpointer({"current_goal": None, "active_form": None})
        orch, store = _orchestrator(ws, checkpointer)
        monkeypatch.setattr(
            orch, "_run_market", lambda *a, **kw: _crashing_run_market_timeout(ws, *a, **kw)
        )
        monkeypatch.setattr(
            "ladini.orchestrator.orchestrator.conversation_turn_lock",
            _noop_lock,
        )

        await orch.handle(phone="+22670000001", user_query="je change d'avis")

        assert store.saved_snapshots, "le workspace aurait dû être flush malgré l'échec"
        persisted = store.saved_snapshots[-1]
        assert persisted.active_goal == "", (
            f"active_goal périmé ('SALES_PUBLISH_PRODUCT') a survécu au timeout au lieu "
            f"d'être réconcilié depuis le checkpoint réel (current_goal=None) : "
            f"{persisted.active_goal!r}"
        )

    @pytest.mark.asyncio
    async def test_a_still_active_goal_in_the_checkpoint_is_preserved_not_wiped(
        self, monkeypatch
    ):
        """Non-régression : la réconciliation ne doit PAS effacer aveuglément
        `active_goal` — si le dernier nœud à avoir réellement fini avant le
        crash montre le but encore ACTIF (ex: en attente d'une info), ce
        but doit survivre pour que l'utilisateur puisse reprendre."""
        ws = _stale_workspace("")
        checkpointer = _FakeCheckpointer(
            {"current_goal": "SALES_PUBLISH_PRODUCT", "active_form": None}
        )
        orch, store = _orchestrator(ws, checkpointer)
        monkeypatch.setattr(
            orch, "_run_market", lambda *a, **kw: _crashing_run_market_timeout(ws, *a, **kw)
        )
        monkeypatch.setattr(
            "ladini.orchestrator.orchestrator.conversation_turn_lock",
            _noop_lock,
        )

        await orch.handle(phone="+22670000001", user_query="12")

        persisted = store.saved_snapshots[-1]
        assert persisted.active_goal == "SALES_PUBLISH_PRODUCT"


class TestStaleActiveGoalIsReconciledOnGenericException:
    @pytest.mark.asyncio
    async def test_active_goal_is_reconciled_on_an_unexpected_node_exception_too(
        self, monkeypatch
    ):
        ws = _stale_workspace("PROCUREMENT_CREATE_REQUEST")
        checkpointer = _FakeCheckpointer({"current_goal": None, "active_form": None})
        orch, store = _orchestrator(ws, checkpointer)
        monkeypatch.setattr(
            orch, "_run_market", lambda *a, **kw: _crashing_run_market_exception(ws, *a, **kw)
        )
        monkeypatch.setattr(
            "ladini.orchestrator.orchestrator.conversation_turn_lock",
            _noop_lock,
        )

        await orch.handle(phone="+22670000001", user_query="bonjour")

        persisted = store.saved_snapshots[-1]
        assert persisted.active_goal == ""


class TestReconciliationNeverBreaksTheFailureResponse:
    @pytest.mark.asyncio
    async def test_the_fallback_response_is_still_returned_on_timeout(self, monkeypatch):
        """La réconciliation est un effet de bord sur `ws` — elle ne doit
        jamais changer le contrat de retour existant (message de repli)."""
        ws = _stale_workspace("SALES_PUBLISH_PRODUCT")
        checkpointer = _FakeCheckpointer({"current_goal": None})
        orch, _store = _orchestrator(ws, checkpointer)
        monkeypatch.setattr(
            orch, "_run_market", lambda *a, **kw: _crashing_run_market_timeout(ws, *a, **kw)
        )
        monkeypatch.setattr(
            "ladini.orchestrator.orchestrator.conversation_turn_lock",
            _noop_lock,
        )

        result = await orch.handle(phone="+22670000001", user_query="je change d'avis")
        assert "final_response" in result
        assert result["workspace_id"] == "+22670000001"

    @pytest.mark.asyncio
    async def test_reconciliation_failure_itself_never_crashes_the_turn(self, monkeypatch):
        """Best-effort par construction (comme `_load_before_state`) : si la
        relecture du checkpoint échoue à son tour, le tour doit quand même
        se terminer proprement plutôt que de remonter une 2e exception."""
        ws = _stale_workspace("SALES_PUBLISH_PRODUCT")

        class _BrokenCheckpointer(_FakeCheckpointer):
            async def aget_tuple(self, _config: Any) -> _FakeCheckpointTuple:
                raise RuntimeError("checkpoint store unavailable")

        orch, store = _orchestrator(ws, _BrokenCheckpointer({}))
        monkeypatch.setattr(
            orch, "_run_market", lambda *a, **kw: _crashing_run_market_timeout(ws, *a, **kw)
        )
        monkeypatch.setattr(
            "ladini.orchestrator.orchestrator.conversation_turn_lock",
            _noop_lock,
        )

        result = await orch.handle(phone="+22670000001", user_query="je change d'avis")
        assert "final_response" in result
        # `active_goal` reste à sa valeur d'avant (best-effort échoué) —
        # dégradation, jamais un crash.
        assert store.saved_snapshots[-1].active_goal == "SALES_PUBLISH_PRODUCT"


@asynccontextmanager
async def _noop_lock(_workspace_id: str, *, timeout_seconds: float):
    yield True
