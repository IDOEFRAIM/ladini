"""Cron : vider ``analytics.event_outbox`` vers ``analytics.business_events``."""

from __future__ import annotations

import logging

from ladini.api.celery_app import celery_app
from ladini.workers.outbox.analytics_dispatcher import AnalyticsEventDispatcher
from ladini.workers.runtime import run_async

logger = logging.getLogger("Ladini.Workers.Cron.AnalyticsEventDrain")


async def _run(batch_size: int) -> dict:
    report = await AnalyticsEventDispatcher().run(batch_size=batch_size)
    return report.as_dict()


@celery_app.task(name="workers.analytics_event_drain", bind=True, max_retries=1)
def run_analytics_event_drain_cron(self, batch_size: int = 100) -> dict:
    try:
        return run_async(_run(batch_size))
    except Exception as exc:
        logger.exception("Cron analytics_event_drain en échec")
        raise self.retry(exc=exc, countdown=15) from exc
