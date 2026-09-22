"""Cron : digest quotidien d'approvisionnement récurrent (Phase 4)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ladini.api.celery_app import celery_app
from ladini.core.settings import settings
from ladini.workers.automation.recurring_supply_digest_service import (
    RecurringSupplyDigestService,
)
from ladini.workers.runtime import run_async, worker_session

logger = logging.getLogger("Ladini.Workers.Cron.RecurringSupplyDigest")


async def _run(day_offset: int) -> dict:
    target_date = (datetime.utcnow() + timedelta(days=day_offset)).date()
    async with worker_session() as session:
        batch = await RecurringSupplyDigestService(session).run(target_date=target_date, trigger="scheduled")
    return {
        "occurrence_date": batch.occurrence_date,
        "buyers_examined": batch.buyers_examined,
        "duration_ms": batch.duration_ms,
        "reports": [r.as_dict() for r in batch.reports],
    }


@celery_app.task(name="workers.recurring_supply_digest", bind=True, max_retries=2)
def run_recurring_supply_digest_cron(self, day_offset: int | None = None) -> dict:
    try:
        return run_async(_run(day_offset if day_offset is not None else settings.RECURRING_SUPPLY_DIGEST_DAY_OFFSET))
    except Exception as exc:
        logger.exception("Cron recurring_supply_digest en échec")
        raise self.retry(exc=exc, countdown=120) from exc
