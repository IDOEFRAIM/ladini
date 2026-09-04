"""`PreorderDraft` — persistance transactionnelle réelle (2026-09-03,
migration PREORDER). Gabarit DIRECT de `procurement_draft_store.py` (mandat
§4 : "utilise les conventions déjà établies pour PROCUREMENT, ne construis
pas un système de persistance différent sans justification") — même
schéma, mêmes 4 fonctions, table dédiée.

## Décision (identique à PROCUREMENT, justifiée séparément)

Snapshot versionné, PAS un event log — une seule ligne par `draft_id`,
`version` comme compteur de verrouillage optimiste. Même raisonnement :
`PreorderDraft`/`apply_domain_action` sont déjà un modèle à instantané
unique, et le brouillon local précède la donnée durable réelle
(`Order` en base, déjà persistée par `create_preorder_draft`).

## Ce module N'A PAS de test contre une vraie instance Postgres

Même limite honnête que `procurement_draft_store.py` — faux moteur fidèle
(voir `tests/architecture/test_preorder_draft_persistence.py`), jamais un
vrai Postgres."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy import text

from agriconnect.core.database import get_sessionmaker
from agriconnect.graphs.agents.market_coach.domain.preorder_draft import (
    PreorderDraft,
)
from agriconnect.services.database.draft_store_support import decode_json_payload

logger = logging.getLogger("agriconnect.services.database.preorder_draft_store")

PREORDER_DRAFT_SCHEMA_DDL = (
    "CREATE TABLE IF NOT EXISTS marketplace.preorder_drafts ("
    "draft_id TEXT PRIMARY KEY, "
    "conversation_id TEXT NOT NULL, "
    "version INTEGER NOT NULL, "
    "status TEXT NOT NULL, "
    "order_id TEXT, "
    "payload JSONB NOT NULL, "
    "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
    "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
    ")",
    "CREATE INDEX IF NOT EXISTS ix_preorder_drafts_conversation "
    "ON marketplace.preorder_drafts (conversation_id)",
    # (2026-09-03, clôture escrow/IPN) : `order_id` — l'IPN Paydunya et le
    # cron d'expiration paiement identifient une commande par `Order.id`,
    # jamais par `draft_id` (qu'ils ne connaissent pas). Colonne ajoutée
    # après coup (`ALTER ... ADD COLUMN IF NOT EXISTS`, même convention que
    # `services/database/common.py::SCHEMA_COLUMN_DDL`) plutôt que recréée
    # dans le `CREATE TABLE` ci-dessus : reste idempotent même sur une base
    # où la table existait déjà sans cette colonne.
    "ALTER TABLE marketplace.preorder_drafts ADD COLUMN IF NOT EXISTS order_id TEXT",
    "CREATE INDEX IF NOT EXISTS ix_preorder_drafts_order_id "
    "ON marketplace.preorder_drafts (order_id) WHERE order_id IS NOT NULL",
)

_SELECT_SQL = text(
    "SELECT draft_id, version, status, payload "
    "FROM marketplace.preorder_drafts WHERE draft_id = :draft_id"
)
_SELECT_BY_ORDER_ID_SQL = text(
    "SELECT draft_id, version, status, payload "
    "FROM marketplace.preorder_drafts WHERE order_id = :order_id"
)
_INSERT_SQL = text(
    "INSERT INTO marketplace.preorder_drafts "
    "(draft_id, conversation_id, version, status, order_id, payload, created_at, updated_at) "
    "VALUES (:draft_id, :conversation_id, :version, :status, :order_id, :payload, now(), now()) "
    "ON CONFLICT (draft_id) DO NOTHING"
)
_CAS_UPDATE_SQL = text(
    "UPDATE marketplace.preorder_drafts "
    "SET version = :new_version, status = :status, order_id = :order_id, "
    "payload = :payload, updated_at = now() "
    "WHERE draft_id = :draft_id AND version = :expected_version"
)
_SELECT_STALE_BY_STATUS_SQL = text(
    "SELECT draft_id, version, status, payload FROM marketplace.preorder_drafts "
    "WHERE status = :status AND updated_at < now() - (:older_than_seconds || ' seconds')::interval"
)


def _row_to_draft(row: Any) -> Optional[PreorderDraft]:
    payload = decode_json_payload(row.payload)
    payload["draft_id"] = row.draft_id
    payload["version"] = row.version
    payload["status"] = row.status
    return PreorderDraft.from_dict(payload)


async def load(draft_id: str) -> Optional[PreorderDraft]:
    """Lecture AUTORITATIVE — jamais depuis le cache LangGraph. Best-effort."""
    try:
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("PREORDER_DRAFT_STORE_NO_SESSIONMAKER")
            return None
        async with sessionmaker() as session:
            result = await session.execute(_SELECT_SQL, {"draft_id": draft_id})
            row = result.mappings().first()
            if row is None:
                return None
            return _row_to_draft(row)
    except Exception:
        logger.exception("PREORDER_DRAFT_LOAD_ERROR | draft_id=%s", draft_id)
        return None


async def find_by_order_id(order_id: str) -> Optional[PreorderDraft]:
    """Lecture AUTORITATIVE par `Order.id` — utilisée par l'IPN Paydunya et
    le cron d'expiration paiement, qui ne connaissent JAMAIS `draft_id`
    (mandat §6, clôture escrow/IPN). Best-effort, `None` si injoignable OU
    si aucun draft ne correspond (ex: une commande créée hors du tunnel
    PREORDER — gagnant d'enchère converti en commande — n'a jamais eu de
    `PreorderDraft` : ce n'est PAS une erreur, l'appelant doit traiter ce
    cas comme un no-op, jamais un crash)."""
    try:
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("PREORDER_DRAFT_STORE_NO_SESSIONMAKER")
            return None
        async with sessionmaker() as session:
            result = await session.execute(_SELECT_BY_ORDER_ID_SQL, {"order_id": order_id})
            row = result.mappings().first()
            if row is None:
                return None
            return _row_to_draft(row)
    except Exception:
        logger.exception("PREORDER_DRAFT_FIND_BY_ORDER_ID_ERROR | order_id=%s", order_id)
        return None


async def insert(draft: PreorderDraft, *, conversation_id: str) -> bool:
    """INSERT initial (v1) — best-effort."""
    try:
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("PREORDER_DRAFT_STORE_NO_SESSIONMAKER")
            return False
        async with sessionmaker() as session:
            result = await session.execute(
                _INSERT_SQL,
                {
                    "draft_id": draft.draft_id,
                    "conversation_id": conversation_id,
                    "version": draft.version,
                    "status": draft.status.value,
                    "order_id": draft.order_id,
                    "payload": json.dumps(draft.to_dict()),
                },
            )
            await session.commit()
            return result.rowcount == 1
    except Exception:
        logger.exception("PREORDER_DRAFT_INSERT_ERROR | draft_id=%s", draft.draft_id)
        return False


async def compare_and_swap(
    draft_id: str, *, expected_version: int, new_draft: PreorderDraft
) -> bool:
    """LE point d'atomicité transactionnelle : `UPDATE ... WHERE
    version = :expected_version`, `rowcount` vérifié — jamais un verrou
    Python."""
    try:
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("PREORDER_DRAFT_STORE_NO_SESSIONMAKER")
            return False
        async with sessionmaker() as session:
            result = await session.execute(
                _CAS_UPDATE_SQL,
                {
                    "draft_id": draft_id,
                    "expected_version": expected_version,
                    "new_version": new_draft.version,
                    "status": new_draft.status.value,
                    "order_id": new_draft.order_id,
                    "payload": json.dumps(new_draft.to_dict()),
                },
            )
            await session.commit()
            return result.rowcount == 1
    except Exception:
        logger.exception(
            "PREORDER_DRAFT_CAS_ERROR | draft_id=%s | expected_version=%s",
            draft_id,
            expected_version,
        )
        return False


async def find_stale_by_status(status: str, *, older_than_seconds: float) -> list:
    """Généralisation (mandat clôture escrow/IPN) — `find_stale_executing`
    devient un cas particulier de celle-ci (`status="EXECUTING"`), utilisée
    aussi pour `status="AWAITING_PAYMENT"` (réconciliation paiement, section
    G du rapport). Réutilise `updated_at` comme `started_at` implicite
    (même raisonnement que `procurement_draft_store.py`) — AUCUN nouveau
    champ dupliqué pour représenter la même notion."""
    try:
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("PREORDER_DRAFT_STORE_NO_SESSIONMAKER")
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
            "PREORDER_DRAFT_FIND_STALE_BY_STATUS_ERROR | status=%s | older_than_seconds=%s",
            status,
            older_than_seconds,
        )
        return []


async def find_stale_executing(*, older_than_seconds: float) -> list:
    """Alias historique (`status="EXECUTING"` seul) — conservé pour ne pas
    casser `test_preorder_draft_persistence.py`/les appelants existants."""
    return await find_stale_by_status("EXECUTING", older_than_seconds=older_than_seconds)


__all__ = [
    "PREORDER_DRAFT_SCHEMA_DDL",
    "load",
    "find_by_order_id",
    "insert",
    "compare_and_swap",
    "find_stale_by_status",
    "find_stale_executing",
]
