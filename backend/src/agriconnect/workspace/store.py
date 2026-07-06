"""WorkspaceStore — persistance Postgres unique des Workspaces.

Source de vérité durable. Aucun checkpointing externe : l'état agent vit
dans la colonne JSONB `metadata`. Table créée de façon idempotente au boot.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy import text

from agriconnect.core.database import get_db
from agriconnect.workspace.metadata import LANGGRAPH_STATE_KEY, filter_metadata_dict
from agriconnect.workspace.models import Workspace

logger = logging.getLogger("AgriConnect.Workspace.Store")

_DDL = """
CREATE TABLE IF NOT EXISTS agri_workspaces (
    workspace_id     TEXT PRIMARY KEY,
    workspace_type   TEXT NOT NULL DEFAULT 'producer',
    active_agent     TEXT NOT NULL DEFAULT 'market',
    active_goal      TEXT NOT NULL DEFAULT '',
    active_form      TEXT,
    locked_agent     TEXT,
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    langgraph_state  JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at       DOUBLE PRECISION NOT NULL DEFAULT 0
);
"""

_ALTERS = [
    "ALTER TABLE agri_workspaces ADD COLUMN IF NOT EXISTS locked_agent TEXT;",
    "ALTER TABLE agri_workspaces ADD COLUMN IF NOT EXISTS langgraph_state JSONB NOT NULL DEFAULT '{}'::jsonb;",
]

_UPSERT = """
INSERT INTO agri_workspaces
    (workspace_id, workspace_type, active_agent, active_goal, active_form, locked_agent, metadata, langgraph_state, updated_at)
VALUES
    (:workspace_id, :workspace_type, :active_agent, :active_goal, :active_form, :locked_agent,
     CAST(:metadata AS JSONB), CAST(:langgraph_state AS JSONB), :updated_at)
ON CONFLICT (workspace_id) DO UPDATE SET
    workspace_type  = EXCLUDED.workspace_type,
    active_agent    = EXCLUDED.active_agent,
    active_goal     = EXCLUDED.active_goal,
    active_form     = EXCLUDED.active_form,
    locked_agent    = EXCLUDED.locked_agent,
    metadata        = EXCLUDED.metadata,
    langgraph_state = EXCLUDED.langgraph_state,
    updated_at      = EXCLUDED.updated_at;
"""

_SELECT = "SELECT * FROM agri_workspaces WHERE workspace_id = :workspace_id;"


class WorkspaceStore:
    """CRUD minimal Postgres pour les Workspaces."""

    _table_ready = False

    async def _ensure_table(self) -> None:
        if WorkspaceStore._table_ready:
            return
        try:
            async with get_db() as session:
                await session.execute(text(_DDL))
                for ddl in _ALTERS:
                    await session.execute(text(ddl))
            WorkspaceStore._table_ready = True
        except Exception as exc:  # pragma: no cover - infra
            logger.error("WorkspaceStore table creation failed: %s", exc)

    async def get(self, workspace_id: str) -> Optional[Workspace]:
        await self._ensure_table()
        async with get_db() as session:
            row = (
                await session.execute(text(_SELECT), {"workspace_id": workspace_id})
            ).mappings().first()
        if row is None:
            return None
        data = dict(row)
        raw_meta = data.get("metadata")
        if isinstance(raw_meta, str):
            raw_meta = json.loads(raw_meta or "{}")
        filtered_meta = filter_metadata_dict(raw_meta)
        data["metadata"] = filtered_meta

        raw_state = data.get("langgraph_state")
        if isinstance(raw_state, str):
            raw_state = json.loads(raw_state or "{}")
        if not raw_state and isinstance(raw_meta, dict):
            legacy_state = raw_meta.get(LANGGRAPH_STATE_KEY)
            if isinstance(legacy_state, dict):
                raw_state = legacy_state
        data["langgraph_state"] = raw_state or {}
        return Workspace.from_dict(data)

    async def save(self, workspace: Workspace) -> None:
        await self._ensure_table()
        workspace.touch()
        try:
            async with get_db() as session:
                filtered_meta = filter_metadata_dict(workspace.metadata)
                meta_json = json.dumps(filtered_meta)
                state_payload = workspace.agent_state or {}
                if not state_payload and isinstance(workspace.metadata, dict):
                    legacy_state = workspace.metadata.get(LANGGRAPH_STATE_KEY)
                    if isinstance(legacy_state, dict):
                        state_payload = legacy_state
                state_json = json.dumps(state_payload or {})
                await session.execute(
                    text(_UPSERT),
                    {
                        "workspace_id": workspace.workspace_id,
                        "workspace_type": workspace.workspace_type,
                        "active_agent": workspace.active_agent,
                        "active_goal": workspace.active_goal,
                        "active_form": workspace.active_form,
                        "locked_agent": workspace.locked_agent,
                        "metadata": meta_json,
                        "langgraph_state": state_json,
                        "updated_at": workspace.updated_at,
                    },
                )
                meta_size = len(meta_json.encode("utf-8"))
                if meta_size > 50_000:
                    logger.critical("Workspace metadata exceeds 50KB after save (id=%s size=%s)", workspace.workspace_id, meta_size)
        except Exception as exc:
            logger.error("WorkspaceStore.save(%s) failed: %s", workspace.workspace_id, exc)
