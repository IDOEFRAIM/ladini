"""`SalesPublishDraft` — persistance transactionnelle réelle (2026-09-04,
migration SALES). Gabarit DIRECT de `procurement_draft_store.py`/
`preorder_draft_store.py` (mandat §11 : "ne crée pas un quatrième pattern
de persistance") — même schéma, mêmes fonctions, table dédiée. Réutilise
`decode_json_payload`/`cas_finalize` (`draft_store_support.py`, extraits
pendant le hardening transverse PRÉCISÉMENT pour que ce 3e store n'ait pas
à redupliquer ce qui l'était déjà 2 fois)."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy import text

from ladini.core.database import get_sessionmaker
from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
    SalesPublishDraft,
)
from ladini.services.database.draft_store_support import decode_json_payload

logger = logging.getLogger("ladini.services.database.sales_publish_draft_store")

_SELECT_SQL = text(
    "SELECT draft_id, version, status, payload "
    "FROM marketplace.sales_publish_drafts WHERE draft_id = :draft_id"
)
_INSERT_SQL = text(
    "INSERT INTO marketplace.sales_publish_drafts "
    "(draft_id, conversation_id, version, status, payload, created_at, updated_at) "
    "VALUES (:draft_id, :conversation_id, :version, :status, :payload, now(), now()) "
    "ON CONFLICT (draft_id) DO NOTHING"
)
_CAS_UPDATE_SQL = text(
    "UPDATE marketplace.sales_publish_drafts "
    "SET version = :new_version, status = :status, payload = :payload, updated_at = now() "
    "WHERE draft_id = :draft_id AND version = :expected_version"
)
_SELECT_STALE_BY_STATUS_SQL = text(
    "SELECT draft_id, version, status, payload FROM marketplace.sales_publish_drafts "
    "WHERE status = :status AND updated_at < now() - make_interval(secs => :older_than_seconds)"
)


def _row_to_draft(row: Any) -> Optional[SalesPublishDraft]:
    payload = decode_json_payload(row.payload)
    payload["draft_id"] = row.draft_id
    payload["version"] = row.version
    payload["status"] = row.status
    return SalesPublishDraft.from_dict(payload)


async def load(draft_id: str) -> Optional[SalesPublishDraft]:
    """Lecture AUTORITATIVE — jamais depuis le cache LangGraph. Best-effort."""
    try:
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("SALES_PUBLISH_DRAFT_STORE_NO_SESSIONMAKER")
            return None
        async with sessionmaker() as session:
            result = await session.execute(_SELECT_SQL, {"draft_id": draft_id})
            row = result.mappings().first()
            if row is None:
                return None
            return _row_to_draft(row)
    except Exception:
        logger.exception("SALES_PUBLISH_DRAFT_LOAD_ERROR | draft_id=%s", draft_id)
        return None


async def insert(draft: SalesPublishDraft, *, conversation_id: str) -> bool:
    """INSERT initial (v1) — best-effort."""
    try:
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("SALES_PUBLISH_DRAFT_STORE_NO_SESSIONMAKER")
            return False
        async with sessionmaker() as session:
            result = await session.execute(
                _INSERT_SQL,
                {
                    "draft_id": draft.draft_id,
                    "conversation_id": conversation_id,
                    "version": draft.version,
                    "status": draft.status.value,
                    "payload": json.dumps(draft.to_dict()),
                },
            )
            await session.commit()
            return result.rowcount == 1
    except Exception:
        logger.exception("SALES_PUBLISH_DRAFT_INSERT_ERROR | draft_id=%s", draft.draft_id)
        return False


async def compare_and_swap(
    draft_id: str, *, expected_version: int, new_draft: SalesPublishDraft
) -> bool:
    """LE point d'atomicité transactionnelle : `UPDATE ... WHERE
    version = :expected_version`, `rowcount` vérifié — jamais un verrou
    Python."""
    try:
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("SALES_PUBLISH_DRAFT_STORE_NO_SESSIONMAKER")
            return False
        async with sessionmaker() as session:
            result = await session.execute(
                _CAS_UPDATE_SQL,
                {
                    "draft_id": draft_id,
                    "expected_version": expected_version,
                    "new_version": new_draft.version,
                    "status": new_draft.status.value,
                    "payload": json.dumps(new_draft.to_dict()),
                },
            )
            await session.commit()
            return result.rowcount == 1
    except Exception:
        logger.exception(
            "SALES_PUBLISH_DRAFT_CAS_ERROR | draft_id=%s | expected_version=%s",
            draft_id,
            expected_version,
        )
        return False


async def find_stale_by_status(status: str, *, older_than_seconds: float) -> list:
    """Requête de RÉCONCILIATION — même convention que PROCUREMENT/PREORDER
    (`updated_at` sert de `started_at` implicite pour un statut qui n'a que
    des transitions SORTANTES)."""
    try:
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("SALES_PUBLISH_DRAFT_STORE_NO_SESSIONMAKER")
            return []
        async with sessionmaker() as session:
            result = await session.execute(
                _SELECT_STALE_BY_STATUS_SQL,
                {"status": status, "older_than_seconds": older_than_seconds},
            )
            rows = result.mappings().all()
            return [d for d in (_row_to_draft(row) for row in rows) if d is not None]
    except Exception:
        logger.exception(
            "SALES_PUBLISH_DRAFT_FIND_STALE_BY_STATUS_ERROR | status=%s | older_than_seconds=%s",
            status,
            older_than_seconds,
        )
        return []


__all__ = [
    "load",
    "insert",
    "compare_and_swap",
    "find_stale_by_status",
]
