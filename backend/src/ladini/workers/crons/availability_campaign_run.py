"""Cron : exécute les campagnes de disponibilités dues (réclamation SKIP LOCKED, lots bornés, reprise sûre)."""

from __future__ import annotations

import logging

from ladini.api.celery_app import celery_app
from ladini.services.availability_campaigns.campaign_service import run_due_campaigns
from ladini.workers.runtime import run_async

logger = logging.getLogger("Ladini.Workers.Cron.AvailabilityCampaignRun")


@celery_app.task(name="workers.availability_campaign_run", bind=True, max_retries=1)
def run_availability_campaign_cron(self) -> dict:
    try:
        report: dict = run_async(run_due_campaigns())
        return report
    except Exception as exc:
        logger.exception("Cron availability_campaign_run en échec")
        raise self.retry(exc=exc, countdown=30) from exc
