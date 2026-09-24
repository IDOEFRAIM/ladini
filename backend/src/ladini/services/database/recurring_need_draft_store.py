"""`RecurringNeedDraft` — persistance PostgreSQL réelle (`marketplace.recurring_need_drafts`).

La table existait déjà (migration Drizzle `0003_recurring_need_drafts`, miroir
`domain/runtime_tables.py`) mais AUCUN code n'y écrivait : le draft vivait uniquement dans
le blob d'état LangGraph (audit 2026-09-24, P0). Même modèle que
`procurement_draft_store.py` : UNE ligne par `draft_id` (l'état courant), `version` comme
compteur de verrouillage optimiste, `compare_and_swap` = `UPDATE ... WHERE version =
:expected` avec `rowcount` vérifié.

Rôle supplémentaire, propre à ce draft : la ligne est aussi le REGISTRE DURABLE de la
confirmation. `services/database/recurring_supply.py` la verrouille (`FOR UPDATE`) et la
passe à `EXECUTED` DANS LA MÊME TRANSACTION que l'insertion des `recurring_needs` — une
confirmation `(draft_id, execution_version)` ne peut donc produire qu'un seul jeu de
besoins, quels que soient les retries (HTTP, Celery, timeout MCP, « oui » répété).

Best-effort comme les autres stores : aucune fonction ne lève ; `None`/`False`/`[]` si la
base est indisponible. C'est l'APPELANT qui décide si une écriture manquée est tolérable
(édition du draft : oui, mode dégradé) ou bloquante (passage en EXECUTING : non).
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Optional

from sqlalchemy import text

from ladini.core.database import get_sessionmaker
from ladini.graphs.agents.market_coach.domain.recurring_need_draft import (
    RecurringNeedDraft,
)
from ladini.services.database.draft_store_support import decode_json_payload

logger = logging.getLogger("ladini.services.database.recurring_need_draft_store")

_SELECT_SQL = text(
    "SELECT draft_id, version, status, payload "
    "FROM marketplace.recurring_need_drafts WHERE draft_id = :draft_id"
)
_INSERT_SQL = text(
    "INSERT INTO marketplace.recurring_need_drafts "
    "(draft_id, conversation_id, version, status, payload, created_at, updated_at) "
    "VALUES (:draft_id, :conversation_id, :version, :status, :payload, now(), now()) "
    "ON CONFLICT (draft_id) DO NOTHING"
)
_CAS_UPDATE_SQL = text(
    "UPDATE marketplace.recurring_need_drafts "
    "SET version = :new_version, status = :status, payload = :payload, updated_at = now() "
    "WHERE draft_id = :draft_id AND version = :expected_version"
)
_SELECT_IN_DOUBT_SQL = text(
    "SELECT draft_id, conversation_id, version, status, payload "
    "FROM marketplace.recurring_need_drafts "
    "WHERE status IN ('EXECUTING', 'EXECUTION_UNKNOWN') "
    "AND updated_at < now() - make_interval(secs => :older_than_seconds)"
)
_SELECT_ABANDONED_SQL = text(
    "SELECT draft_id, conversation_id, version, status, payload "
    "FROM marketplace.recurring_need_drafts "
    "WHERE status = 'DRAFT' "
    "AND updated_at < now() - make_interval(secs => :older_than_seconds)"
)


def _row_to_draft(row: Any) -> Optional[RecurringNeedDraft]:
    payload = decode_json_payload(row["payload"] if isinstance(row, dict) else row.payload)
    payload["draft_id"] = row["draft_id"]
    payload["version"] = row["version"]
    payload["status"] = row["status"]
    return RecurringNeedDraft.from_dict(payload)


def _encode(draft: RecurringNeedDraft) -> str:
    return json.dumps(draft.to_dict())


async def load(draft_id: str) -> Optional[RecurringNeedDraft]:
    """Lecture AUTORITATIVE (jamais depuis le cache LangGraph)."""
    try:
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("RECURRING_DRAFT_STORE_NO_SESSIONMAKER")
            return None
        async with sessionmaker() as session:
            row = (await session.execute(_SELECT_SQL, {"draft_id": draft_id})).mappings().first()
            return _row_to_draft(dict(row)) if row is not None else None
    except Exception:
        logger.exception("RECURRING_DRAFT_LOAD_ERROR | draft_id=%s", draft_id)
        return None


async def insert(draft: RecurringNeedDraft, *, conversation_id: str) -> bool:
    try:
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("RECURRING_DRAFT_STORE_NO_SESSIONMAKER")
            return False
        async with sessionmaker() as session:
            result = await session.execute(
                _INSERT_SQL,
                {
                    "draft_id": draft.draft_id,
                    "conversation_id": conversation_id,
                    "version": draft.version,
                    "status": draft.status.value,
                    "payload": _encode(draft),
                },
            )
            await session.commit()
            return bool(result.rowcount == 1)
    except Exception:
        logger.exception("RECURRING_DRAFT_INSERT_ERROR | draft_id=%s", draft.draft_id)
        return False


async def compare_and_swap(
    draft_id: str, *, expected_version: int, new_draft: RecurringNeedDraft
) -> bool:
    """`True` seulement si CETTE écriture a gagné (`rowcount == 1`)."""
    try:
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("RECURRING_DRAFT_STORE_NO_SESSIONMAKER")
            return False
        async with sessionmaker() as session:
            result = await session.execute(
                _CAS_UPDATE_SQL,
                {
                    "draft_id": draft_id,
                    "expected_version": expected_version,
                    "new_version": new_draft.version,
                    "status": new_draft.status.value,
                    "payload": _encode(new_draft),
                },
            )
            await session.commit()
            return bool(result.rowcount == 1)
    except Exception:
        logger.exception(
            "RECURRING_DRAFT_CAS_ERROR | draft_id=%s | expected_version=%s", draft_id, expected_version
        )
        return False


async def _select_many(sql: Any, older_than_seconds: float) -> List[tuple]:
    try:
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("RECURRING_DRAFT_STORE_NO_SESSIONMAKER")
            return []
        async with sessionmaker() as session:
            rows = (await session.execute(sql, {"older_than_seconds": older_than_seconds})).mappings().all()
        out = []
        for row in rows:
            draft = _row_to_draft(dict(row))
            if draft is not None:
                out.append((draft, row["conversation_id"]))
        return out
    except Exception:
        logger.exception("RECURRING_DRAFT_SELECT_ERROR | older_than_seconds=%s", older_than_seconds)
        return []


async def find_in_doubt(*, older_than_seconds: float) -> List[tuple]:
    """Drafts dont l'exécution a été lancée sans issue connue depuis plus de
    `older_than_seconds` : `[(draft, conversation_id)]`, pour la réconciliation."""
    return await _select_many(_SELECT_IN_DOUBT_SQL, older_than_seconds)


async def find_abandoned(*, older_than_seconds: float) -> List[tuple]:
    """Drafts encore éditables (`DRAFT`) sans activité depuis `older_than_seconds`."""
    return await _select_many(_SELECT_ABANDONED_SQL, older_than_seconds)


__all__ = ["load", "insert", "compare_and_swap", "find_in_doubt", "find_abandoned"]
