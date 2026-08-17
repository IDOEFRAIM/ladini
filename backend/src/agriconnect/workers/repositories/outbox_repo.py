"""Repository ``notification_outbox`` — file d'attente durable de messages.

Deux garde-fous d'idempotence :
* ``dedupe_key`` unique → ``ON CONFLICT DO NOTHING`` : jamais deux fois le même
  message même si un cron rejoue.
* claim ``FOR UPDATE SKIP LOCKED`` → deux dispatchers ne prennent jamais la
  même ligne (concurrence sûre).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agriconnect.domain.models import NotificationOutbox

# Backoff exponentiel (minutes) indexé par numéro de tentative.
_BACKOFF_MINUTES = [1, 5, 15, 60, 180]


def backoff_delay(attempts: int) -> timedelta:
    idx = min(max(attempts - 1, 0), len(_BACKOFF_MINUTES) - 1)
    return timedelta(minutes=_BACKOFF_MINUTES[idx])


async def enqueue(session: AsyncSession, entries: Sequence[Dict[str, Any]]) -> int:
    """Insère des messages dans l'outbox en ignorant les doublons (dedupe_key).

    Retourne le nombre de lignes réellement insérées.
    """
    if not entries:
        return 0
    stmt = (
        pg_insert(NotificationOutbox)
        .values(list(entries))
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(NotificationOutbox.id)
    )
    result = await session.execute(stmt)
    return len(result.all())


async def claim_due(
    session: AsyncSession, *, limit: int = 50
) -> List[NotificationOutbox]:
    """Réserve un lot de messages « dus » et les passe à SENDING (atomique).

    À appeler dans sa PROPRE transaction, committée avant tout envoi externe :
    ainsi un crash pendant l'envoi ne perd pas l'état et un autre worker ne
    reprend pas les mêmes lignes.
    """
    now = datetime.utcnow()
    stmt = (
        select(NotificationOutbox)
        .where(
            NotificationOutbox.status == "PENDING",
            NotificationOutbox.next_attempt_at <= now,
        )
        .order_by(NotificationOutbox.next_attempt_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    for row in rows:
        row.status = "SENDING"
    await session.flush()
    return rows


async def mark_sent(session: AsyncSession, outbox_id: Any) -> None:
    row = await session.get(NotificationOutbox, outbox_id)
    if row is None:
        return
    row.status = "SENT"
    row.sent_at = datetime.utcnow()
    row.attempts = int(row.attempts or 0) + 1
    row.last_error = None


async def mark_failed(session: AsyncSession, outbox_id: Any, *, error: str) -> None:
    """Réarme (PENDING + backoff) ou déclare DEAD si le plafond est atteint."""
    row = await session.get(NotificationOutbox, outbox_id)
    if row is None:
        return
    attempts = int(row.attempts or 0) + 1
    row.attempts = attempts
    row.last_error = (error or "")[:500]
    if attempts >= int(row.max_attempts or 5):
        row.status = "DEAD"
    else:
        row.status = "PENDING"
        row.next_attempt_at = datetime.utcnow() + backoff_delay(attempts)
