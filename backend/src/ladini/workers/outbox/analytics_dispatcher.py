"""AnalyticsEventDispatcher — drains ``analytics.event_outbox`` into
``analytics.business_events``. Same two-phase shape as
``workers/outbox/dispatcher.py`` (notification outbox):

    Phase 1 (one transaction): reserve a batch (status -> SENDING), commit.
    Phase 2 (one transaction per row): INSERT into business_events
        (ON CONFLICT idempotency_key DO NOTHING — belt-and-suspenders with
        the outbox's own dedupe_key uniqueness), then mark SENT or FAILED
        (+ backoff). No external call anywhere in this path — the only
        "phase 2" work is a second same-database INSERT.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy.dialects.postgresql import insert as pg_insert

from ladini.domain.analytics.models import BusinessEventRecord
from ladini.workers.repositories import analytics_outbox_repo
from ladini.workers.runtime import worker_session

logger = logging.getLogger("Ladini.Workers.AnalyticsEventDispatcher")


@dataclass
class AnalyticsDrainReport:
    claimed: int = 0
    landed: int = 0
    failed: int = 0
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"claimed": self.claimed, "landed": self.landed, "failed": self.failed, "errors": self.errors}


def _to_job(row: Any) -> Dict[str, Any]:
    """Freeze what's needed before the claim session closes."""
    return {"id": row.id, "payload": dict(row.payload or {})}


_DATETIME_FIELDS = ("occurred_at", "created_at")


def _payload_to_record_kwargs(payload: Dict[str, Any]) -> Dict[str, Any]:
    kwargs = dict(payload)
    kwargs.pop("created_at", None)  # server_default=now() — always the LANDING time, not the fact time
    for field_name in _DATETIME_FIELDS:
        value = kwargs.get(field_name)
        if isinstance(value, str):
            kwargs[field_name] = datetime.fromisoformat(value)
    return kwargs


class AnalyticsEventDispatcher:
    async def run(self, *, batch_size: int = 100) -> AnalyticsDrainReport:
        report = AnalyticsDrainReport()

        async with worker_session() as session:
            claimed = await analytics_outbox_repo.claim_due(session, limit=batch_size)
            jobs = [_to_job(row) for row in claimed]
        report.claimed = len(jobs)

        for job in jobs:
            try:
                async with worker_session() as session:
                    stmt = (
                        pg_insert(BusinessEventRecord)
                        .values(**_payload_to_record_kwargs(job["payload"]))
                        .on_conflict_do_nothing(index_elements=["idempotency_key"])
                    )
                    await session.execute(stmt)
                    await analytics_outbox_repo.mark_sent(session, job["id"])
                report.landed += 1
            except Exception as exc:  # noqa: BLE001 — one bad row must not stop the batch
                logger.exception("analytics_event_drain | landing failed | outbox_id=%s", job["id"])
                async with worker_session() as session:
                    await analytics_outbox_repo.mark_failed(session, job["id"], error=str(exc))
                report.failed += 1
                report.errors.append(f"{job['id']}: {exc}")

        logger.info("AnalyticsEventDispatcher | %s", report.as_dict())
        return report


__all__ = ["AnalyticsEventDispatcher", "AnalyticsDrainReport"]
