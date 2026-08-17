"""Cron : vider l'outbox — envoie les notifications dues via les canaux."""

from __future__ import annotations

import logging

from agriconnect.api.celery_app import celery_app
from agriconnect.workers.outbox.dispatcher import OutboxDispatcher
from agriconnect.workers.runtime import run_async

logger = logging.getLogger("AgriConnect.Workers.Cron.OutboxDispatch")


async def _run(batch_size: int) -> dict:
    report = await OutboxDispatcher().run(batch_size=batch_size)
    return report.as_dict()


@celery_app.task(name="workers.outbox_dispatch", bind=True, max_retries=1)
def run_outbox_dispatch_cron(self, batch_size: int = 50) -> dict:
    try:
        return run_async(_run(batch_size))
    except Exception as exc:
        logger.exception("Cron outbox_dispatch en échec")
        raise self.retry(exc=exc, countdown=15) from exc
