"""Repository ``analytics.event_outbox`` — same discipline as
``workers/repositories/outbox_repo.py`` (``notification_outbox``), a
dedicated table because the notification outbox's shape is delivery-channel-
specific (WhatsApp/Email templates), not a typed business fact — see the
Phase A/B audit and ``docs/analytics/BUYER_ANALYTICS_ARCHITECTURE.md``.

Two safeguards, identical to the notification outbox:
* ``dedupe_key`` unique -> ``ON CONFLICT DO NOTHING``: the SAME business
  fact (same idempotency key) never queues twice, even on a WhatsApp/Celery
  replay.
* claim ``FOR UPDATE SKIP LOCKED``: two drain workers never grab the same row.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ladini.domain.analytics.models import EventOutboxRecord

_BACKOFF_MINUTES = [1, 5, 15, 60, 180]


def backoff_delay(attempts: int) -> timedelta:
    idx = min(max(attempts - 1, 0), len(_BACKOFF_MINUTES) - 1)
    return timedelta(minutes=_BACKOFF_MINUTES[idx])


async def enqueue(session: AsyncSession, *, event_name: str, journey: str, payload: Dict[str, Any], dedupe_key: str) -> bool:
    """Insert one event-intent row in the CALLER's own transaction. Returns
    whether a new row was actually inserted (False = already queued/landed —
    the caller must not treat that as an error)."""
    stmt = (
        pg_insert(EventOutboxRecord)
        .values(event_name=event_name, journey=journey, payload=payload, dedupe_key=dedupe_key)
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(EventOutboxRecord.id)
    )
    result = await session.execute(stmt)
    return result.first() is not None


async def claim_due(session: AsyncSession, *, limit: int = 100) -> List[EventOutboxRecord]:
    """Reserve a batch of due rows (-> SENDING), atomically. Call in its own
    transaction, committed before draining into business_events — mirrors
    ``outbox_repo.claim_due`` exactly."""
    now = datetime.utcnow()
    stmt = (
        select(EventOutboxRecord)
        .where(EventOutboxRecord.status == "PENDING", EventOutboxRecord.next_attempt_at <= now)
        .order_by(EventOutboxRecord.next_attempt_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    for row in rows:
        row.status = "SENDING"
    await session.flush()
    return rows


async def mark_sent(session: AsyncSession, outbox_id: Any) -> None:
    row = await session.get(EventOutboxRecord, outbox_id)
    if row is None:
        return
    row.status = "SENT"
    row.attempts = int(row.attempts or 0) + 1
    row.last_error = None


async def mark_failed(session: AsyncSession, outbox_id: Any, *, error: str) -> None:
    row = await session.get(EventOutboxRecord, outbox_id)
    if row is None:
        return
    attempts = int(row.attempts or 0) + 1
    row.attempts = attempts
    row.last_error = (error or "")[:500]
    if attempts >= 5:
        row.status = "DEAD"
    else:
        row.status = "PENDING"
        row.next_attempt_at = datetime.utcnow() + backoff_delay(attempts)


__all__ = ["enqueue", "claim_due", "mark_sent", "mark_failed", "backoff_delay"]
