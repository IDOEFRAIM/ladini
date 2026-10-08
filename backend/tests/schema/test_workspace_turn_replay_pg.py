"""Rejeu durable du résultat d'un tour (`agri_workspaces.metadata.last_turn`) contre un VRAI PostgreSQL migré depuis les fichiers officiels.

Garantie testée : le résultat d'un tour est écrit AVEC l'état du workspace (une seule ligne, une seule écriture) ; un worker relancé qui relit la ligne rejoue la réponse
d'origine du MÊME message (id fournisseur) et ne répond jamais pour un autre message.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ladini.orchestrator.orchestrator import _replayed_turn
from ladini.workspace import store as store_module
from ladini.workspace.models import Workspace
from ladini.workspace.store import WorkspaceStore

PHONE = "+22670009999"


def _run_against(dsn: str, scenario):
    async def go():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))

        @asynccontextmanager
        async def _get_db():
            async with AsyncSession(engine) as session:
                try:
                    yield session
                except Exception:
                    await session.rollback()
                    raise

        original = store_module.get_db
        store_module.get_db = _get_db
        try:
            return await scenario(WorkspaceStore())
        finally:
            store_module.get_db = original
            await engine.dispose()

    return asyncio.run(go())


def _workspace(**last_turn) -> Workspace:
    ws = Workspace(workspace_id=PHONE, workspace_type="buyer")
    ws.metadata = {"last_turn": last_turn} if last_turn else {}
    ws.mark_dirty()
    return ws


@pytest.mark.parametrize("fresh_process", [False, True], ids=["same_process", "after_restart"])
def test_the_stored_reply_is_replayed_for_the_same_message_and_only_that_one(pg_dsn, fresh_process):
    async def scenario(store: WorkspaceStore):
        assert await store.save(_workspace(message_sid="wamid.A", final_response="C'est noté !", interactive={"type": "buttons"}))
        reader = WorkspaceStore() if fresh_process else store  # un worker relancé relit la ligne, sans aucun cache
        ws = await reader.get(PHONE)
        replay = _replayed_turn(ws, "wamid.A", PHONE)
        assert replay is not None and replay["final_response"] == "C'est noté !" and replay["interactive"] == {"type": "buttons"}
        assert _replayed_turn(ws, "wamid.B", PHONE) is None, "jamais la réponse d'un autre message"
        assert _replayed_turn(ws, None, PHONE) is None

    _run_against(pg_dsn, scenario)


def test_the_next_turn_replaces_the_previous_replay_window(pg_dsn):
    async def scenario(store: WorkspaceStore):
        await store.save(_workspace(message_sid="wamid.A", final_response="réponse A"))
        await store.save(_workspace(message_sid="wamid.B", final_response="réponse B"))
        ws = await store.get(PHONE)
        assert _replayed_turn(ws, "wamid.B", PHONE)["final_response"] == "réponse B"
        assert _replayed_turn(ws, "wamid.A", PHONE) is None, "la fenêtre de rejeu est le DERNIER tour : l'effet métier plus ancien reste protégé par le domaine"

    _run_against(pg_dsn, scenario)


def test_a_huge_reply_is_bounded_before_it_reaches_the_row(pg_dsn):
    async def scenario(store: WorkspaceStore):
        await store.save(_workspace(message_sid="wamid.BIG", final_response="x" * 20_000))
        ws = await store.get(PHONE)
        assert len(_replayed_turn(ws, "wamid.BIG", PHONE)["final_response"]) == 3500

    _run_against(pg_dsn, scenario)
