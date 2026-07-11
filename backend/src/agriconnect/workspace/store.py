"""WorkspaceStore — persistance Postgres unique des Workspaces.

Source de vérité durable. Aucun checkpointing externe : l'état agent vit
dans la colonne JSONB `langgraph_state`. Table créée de façon idempotente au boot.
"""
from __future__ import annotations

import base64
import json
import logging
import time
import zlib
from typing import Optional, Tuple

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from agriconnect.core.database import get_db
from agriconnect.workspace.metadata import LANGGRAPH_STATE_KEY, filter_metadata_dict
from agriconnect.workspace.models import Workspace

try:  # pragma: no cover - optional asyncpg types for accurate error detection
    import asyncpg  # type: ignore
except Exception:  # pragma: no cover
    asyncpg = None

logger = logging.getLogger("AgriConnect.Workspace.Store")

# ---------------------------------------------------------------------------
# Payload size caps — must be enforced BEFORE sending to Postgres.
# Oversized JSONB payloads cause asyncpg to close the connection mid-transfer.
# ---------------------------------------------------------------------------
_MAX_STATE_BYTES = 480_000   # 480 KB: langgraph_state hard cap
_MAX_META_BYTES  =  48_000   #  48 KB: metadata hard cap
_COMPRESS_THRESHOLD = 100_000  # bytes — start compressing large state blobs

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

_INSERT = """
INSERT INTO agri_workspaces
    (workspace_id, workspace_type, active_agent, active_goal, active_form, locked_agent, metadata, langgraph_state, updated_at)
VALUES
    (:workspace_id, :workspace_type, :active_agent, :active_goal, :active_form, :locked_agent,
     CAST(:metadata AS JSONB), CAST(:langgraph_state AS JSONB), :updated_at);
"""

_UPDATE = """
UPDATE agri_workspaces SET
    workspace_type  = :workspace_type,
    active_agent    = :active_agent,
    active_goal     = :active_goal,
    active_form     = :active_form,
    locked_agent    = :locked_agent,
    metadata        = CAST(:metadata AS JSONB),
    langgraph_state = CAST(:langgraph_state AS JSONB),
    updated_at      = :updated_at
WHERE workspace_id = :workspace_id;
"""

_SELECT = "SELECT * FROM agri_workspaces WHERE workspace_id = :workspace_id;"
_SELECT_FOR_UPDATE = "SELECT workspace_id FROM agri_workspaces WHERE workspace_id = :workspace_id FOR UPDATE;"

_EMPTY_ROW: dict = {
    "active_agent": "market",
    "active_goal": "",
    "active_form": None,
    "locked_agent": None,
    "metadata": json.dumps({}),
    "langgraph_state": json.dumps({}),
}


class WorkspaceStore:
    """CRUD minimal Postgres pour les Workspaces."""

    _table_ready: bool = False

    # ------------------------------------------------------------------
    # Table bootstrap (idempotent, retries on failure)
    # ------------------------------------------------------------------

    async def _ensure_table(self) -> bool:
        if WorkspaceStore._table_ready:
            return True
        try:
            async with get_db() as session:
                await session.execute(text(_DDL))
                for ddl in _ALTERS:
                    await session.execute(text(ddl))
                await session.commit()
            WorkspaceStore._table_ready = True
            return True
        except Exception as exc:
            logger.error("WorkspaceStore table creation failed: %s", exc)
            return False  # Caller gets False; _table_ready stays False so next call retries.

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get(self, workspace_id: str) -> Optional[Workspace]:
        if not await self._ensure_table():
            return None
        try:
            async with get_db() as session:
                row = (
                    await session.execute(text(_SELECT), {"workspace_id": workspace_id})
                ).mappings().first()
        except Exception as exc:
            if self._is_fatal_connection_error(exc):
                logger.warning(
                    "WorkspaceStore.get(%s) fatal DB error — resetting row, returning None: %s",
                    workspace_id, exc,
                )
                await self._reset_workspace_row(workspace_id)
            else:
                logger.error("WorkspaceStore.get(%s) failed: %s", workspace_id, exc)
            return None

        if row is None:
            return None

        data = dict(row)
        raw_meta = data.get("metadata")
        if isinstance(raw_meta, str):
            raw_meta = json.loads(raw_meta or "{}")
        data["metadata"] = filter_metadata_dict(raw_meta)

        raw_state = data.get("langgraph_state")
        if isinstance(raw_state, str):
            raw_state = json.loads(raw_state or "{}")
        decoded_state = _decode_state_blob(raw_state)
        # Legacy fallback: state used to live inside metadata under a special key.
        if not decoded_state and isinstance(raw_meta, dict):
            legacy = raw_meta.get(LANGGRAPH_STATE_KEY)
            if isinstance(legacy, dict):
                decoded_state = legacy
        data["langgraph_state"] = decoded_state or {}

        return Workspace.from_dict(data)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def save(self, workspace: Workspace) -> bool:
        """Persist workspace to Postgres.  Returns True on success, False on any error."""
        if not await self._ensure_table():
            return False

        workspace.touch()

        # ── Metadata ──────────────────────────────────────────────────
        filtered_meta = filter_metadata_dict(workspace.metadata)
        meta_json = json.dumps(filtered_meta)
        meta_bytes = len(meta_json.encode("utf-8"))
        if meta_bytes > _MAX_META_BYTES:
            logger.error(
                "WorkspaceStore.save(%s): metadata too large (%d bytes) — saving empty",
                workspace.workspace_id, meta_bytes,
            )
            meta_json = json.dumps({})

        # ── LangGraph state ───────────────────────────────────────────
        state_payload = workspace.agent_state or {}
        # Legacy: state may still live in metadata on very old rows.
        if not state_payload and isinstance(workspace.metadata, dict):
            legacy = workspace.metadata.get(LANGGRAPH_STATE_KEY)
            if isinstance(legacy, dict):
                state_payload = legacy

        state_json, state_metrics = _encode_state_blob(state_payload or {})
        stored_bytes = len(state_json.encode("utf-8"))
        if stored_bytes > _MAX_STATE_BYTES:
            logger.error(
                "WorkspaceStore.save(%s): compressed state still too large (%d bytes) — persisting summary stub",
                workspace.workspace_id,
                stored_bytes,
            )
            state_json = json.dumps({"__truncated__": True, "original_size": state_metrics.get("raw_bytes", stored_bytes)})
            state_metrics["truncated"] = True
            stored_bytes = len(state_json.encode("utf-8"))

        payload = {
            "workspace_id": workspace.workspace_id,
            "workspace_type": workspace.workspace_type,
            "active_agent": workspace.active_agent,
            "active_goal": workspace.active_goal,
            "active_form": workspace.active_form,
            "locked_agent": workspace.locked_agent,
            "metadata": meta_json,
            "langgraph_state": state_json,
            "updated_at": workspace.updated_at,
        }

        # ── UPSERT ────────────────────────────────────────────────────
        try:
            async with get_db() as session:
                row = (
                    await session.execute(text(_SELECT_FOR_UPDATE), {"workspace_id": workspace.workspace_id})
                ).scalar()
                if row:
                    await session.execute(text(_UPDATE), payload)
                else:
                    await session.execute(text(_INSERT), payload)
                await session.commit()
            logger.info(
                "WorkspaceStore.save blob stats | workspace=%s | raw_bytes=%s | stored_bytes=%s | compressed=%s | truncated=%s",
                workspace.workspace_id,
                state_metrics.get("raw_bytes"),
                stored_bytes,
                state_metrics.get("compressed", False),
                state_metrics.get("truncated", False),
            )
            return True
        except Exception as exc:
            logger.error("WorkspaceStore.save(%s) failed: %s", workspace.workspace_id, exc)
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _reset_workspace_row(
        self,
        workspace_id: str,
        workspace_type: str = "producer",
    ) -> bool:
        """Overwrite a workspace row with a minimal empty state.

        Used when the existing row is unreadable (oversized JSONB causing
        connection drops).  The next ``get()`` will return None, letting the
        resolver reconstruct the workspace from scratch.
        """
        payload = {
            "workspace_id": workspace_id,
            "workspace_type": workspace_type,
            "updated_at": time.time(),
            **_EMPTY_ROW,
        }
        try:
            async with get_db() as session:
                result = await session.execute(text(_UPDATE), payload)
                if not result.rowcount:
                    await session.execute(text(_INSERT), payload)
                await session.commit()
            logger.info("WorkspaceStore: reset workspace %s to empty state", workspace_id)
            return True
        except Exception as exc:
            logger.error("WorkspaceStore: reset failed for %s: %s", workspace_id, exc)
            return False

    @staticmethod
    def _is_fatal_connection_error(exc: Exception) -> bool:
        """Return True when the exception indicates Postgres closed the connection.

        This happens when an oversized JSONB value is streamed: Postgres kills
        the connection mid-transfer.  A fresh pool connection will work fine —
        the problem is the row content, not the pool itself.
        """
        if asyncpg is not None:
            # Unwrap SQLAlchemy dialect wrapper to reach the native asyncpg error.
            base = getattr(exc, "orig", exc)
            _fatal = (
                asyncpg.exceptions.ConnectionDoesNotExistError,
                asyncpg.exceptions.InterfaceError,
                asyncpg.exceptions.ProtocolError,
                asyncpg.exceptions.ProgramLimitExceededError,
            )
            if isinstance(base, _fatal):
                return True
            # SQLAlchemy wraps some asyncpg errors inside its own dialect error.
            if isinstance(exc, SQLAlchemyError):
                inner = getattr(exc, "orig", None)
                if inner is not None and isinstance(inner, _fatal):
                    return True

        # String heuristics when asyncpg types are not importable.
        msg = str(exc).lower()
        return "connection was closed" in msg or ("program" in msg and "limit" in msg)


def _encode_state_blob(payload: dict) -> Tuple[str, dict]:
    """Serialize + optionally compress the langgraph state."""

    json_payload = json.dumps(payload, ensure_ascii=False)
    raw_bytes = len(json_payload.encode("utf-8"))
    metrics = {
        "raw_bytes": raw_bytes,
        "stored_bytes": raw_bytes,
        "compressed": False,
    }

    if raw_bytes <= _COMPRESS_THRESHOLD:
        return json_payload, metrics

    compressed = zlib.compress(json_payload.encode("utf-8"), level=6)
    encoded = base64.b64encode(compressed).decode("ascii")
    wrapper = {
        "__compressed__": True,
        "encoding": "zlib+base64",
        "payload": encoded,
        "original_size": raw_bytes,
        "compressed_size": len(compressed),
    }
    wrapper_json = json.dumps(wrapper)
    metrics.update(
        {
            "stored_bytes": len(wrapper_json.encode("utf-8")),
            "compressed": True,
        }
    )
    return wrapper_json, metrics


def _decode_state_blob(raw_state: Optional[dict]) -> dict:
    if not isinstance(raw_state, dict):
        return raw_state or {}

    if raw_state.get("__compressed__"):
        payload = raw_state.get("payload")
        encoding = raw_state.get("encoding")
        if not isinstance(payload, str) or encoding != "zlib+base64":
            logger.warning("WorkspaceStore: unsupported compressed blob encoding: %s", encoding)
            return {}
        try:
            compressed = base64.b64decode(payload.encode("ascii"))
            decompressed = zlib.decompress(compressed).decode("utf-8")
            return json.loads(decompressed)
        except Exception as exc:
            logger.error("WorkspaceStore: failed to decompress state blob: %s", exc)
            return {}

    if raw_state.get("__truncated__"):
        return {}

    return raw_state
