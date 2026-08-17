"""OutboxDispatcher — lit ``notification_outbox`` et envoie via les canaux.

Deux phases, découplées de la logique métier :

    Phase 1 (une transaction) : réserver un lot (status → SENDING) puis COMMIT.
        → un crash pendant l'envoi ne perd pas l'état ; aucun autre worker ne
          reprend les mêmes lignes (SKIP LOCKED).
    Phase 2 (une transaction par message) : envoyer, puis marquer SENT ou
        FAILED (+ backoff). L'appel externe n'est JAMAIS tenu dans une
        transaction ouverte.

Monitoring : l'état est entièrement porté par l'outbox (``status``, ``attempts``,
``sent_at``) et par ``solicitations`` (``NOTIFIED`` → ``RESPONDED``). Le taux de
conversion = RESPONDED / NOTIFIED se lit directement en SQL, sans table annexe.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agriconnect.workers.outbox import templates
from agriconnect.workers.outbox.channels import build_channel_registry
from agriconnect.workers.repositories import outbox_repo
from agriconnect.workers.runtime import worker_session

logger = logging.getLogger("AgriConnect.Workers.OutboxDispatcher")

# Petite pause entre envois d'un même canal : lisse la charge / respecte les
# quotas provider (ex: Twilio). Ajustable par canal si besoin.
_INTER_SEND_PAUSE_S = 0.15


@dataclass
class DispatchReport:
    claimed: int = 0
    sent: int = 0
    failed: int = 0
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "claimed": self.claimed,
            "sent": self.sent,
            "failed": self.failed,
            "errors": self.errors,
        }


def _to_job(row: Any) -> Dict[str, Any]:
    """Fige les données nécessaires avant fermeture de la session de claim."""
    return {
        "id": row.id,
        "channel": row.channel,
        "recipient_phone": row.recipient_phone,
        "recipient_user_id": row.recipient_user_id,
        "template_key": row.template_key,
        "payload": dict(row.payload or {}),
    }


class OutboxDispatcher:
    def __init__(self, channels: Optional[Dict[str, Any]] = None) -> None:
        self.channels = channels or build_channel_registry()

    async def run(self, *, batch_size: int = 50) -> DispatchReport:
        report = DispatchReport()

        # ── Phase 1 : réservation atomique du lot ──────────────────────────
        async with worker_session() as session:
            claimed = await outbox_repo.claim_due(session, limit=batch_size)
            jobs = [_to_job(row) for row in claimed]
        report.claimed = len(jobs)

        # ── Phase 2 : envoi + mise à jour (une txn courte par message) ─────
        for job in jobs:
            result = await self._deliver(job)
            async with worker_session() as session:
                if result.ok:
                    await outbox_repo.mark_sent(session, job["id"])
                    report.sent += 1
                else:
                    await outbox_repo.mark_failed(
                        session, job["id"], error=result.error or "unknown"
                    )
                    report.failed += 1
                    report.errors.append(f"{job['id']}: {result.error}")
            await asyncio.sleep(_INTER_SEND_PAUSE_S)

        logger.info("OutboxDispatcher | %s", report.as_dict())
        return report

    async def _deliver(self, job: Dict[str, Any]):
        from agriconnect.workers.outbox.channels.base import SendResult

        channel = self.channels.get(str(job.get("channel") or "").upper())
        if channel is None:
            return SendResult.failure(f"unknown_channel:{job.get('channel')}")
        if not channel.is_configured():
            return SendResult.failure(f"channel_not_configured:{job.get('channel')}")

        body = templates.render(job["template_key"], job["payload"])
        return await channel.send(
            body=body,
            recipient_phone=job.get("recipient_phone"),
            recipient_user_id=job.get("recipient_user_id"),
            payload=job.get("payload"),
        )
