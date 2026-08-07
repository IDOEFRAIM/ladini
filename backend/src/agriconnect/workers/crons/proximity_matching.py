"""Cron : notifier les acheteurs locaux des nouveaux produits publiés."""
from __future__ import annotations

import logging

from agriconnect.api.celery_app import celery_app
from agriconnect.workers.automation.proximity_matching_service import (
    ProximityMatchingService,
)
from agriconnect.workers.runtime import run_async, worker_session

logger = logging.getLogger("AgriConnect.Workers.Cron.ProximityMatching")


async def _run(batch_size: int, recent_days: int) -> dict:
    async with worker_session() as session:
        report = await ProximityMatchingService(session).run(
            batch_size=batch_size, recent_days=recent_days
        )
    return report.as_dict()


@celery_app.task(name="workers.proximity_matching", bind=True, max_retries=2)
def run_proximity_matching_cron(self, batch_size: int = 100, recent_days: int = 3) -> dict:
    try:
        return run_async(_run(batch_size, recent_days))
    except Exception as exc:
        logger.exception("Cron proximity_matching en échec")
        raise self.retry(exc=exc, countdown=60)
