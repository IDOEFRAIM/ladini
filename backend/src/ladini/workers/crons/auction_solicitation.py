"""Cron : solliciter les producteurs pertinents pour les enchères ouvertes."""

from __future__ import annotations

import logging

from ladini.api.celery_app import celery_app
from ladini.workers.automation.auction_automation_service import (
    AuctionAutomationService,
)
from ladini.workers.runtime import run_async, worker_session

logger = logging.getLogger("Ladini.Workers.Cron.AuctionSolicitation")


async def _run(batch_size: int) -> dict:
    async with worker_session() as session:
        report = await AuctionAutomationService(session).run(batch_size=batch_size)
    return report.as_dict()


@celery_app.task(name="workers.auction_solicitation", bind=True, max_retries=2)
def run_auction_solicitation_cron(self, batch_size: int = 100) -> dict:
    try:
        return run_async(_run(batch_size))
    except Exception as exc:
        logger.exception("Cron auction_solicitation en échec")
        raise self.retry(exc=exc, countdown=30) from exc
