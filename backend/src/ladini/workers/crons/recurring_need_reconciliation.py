"""Cron : réconciliation des confirmations de besoin récurrent restées en doute.

Aucune logique métier ici — voir `services/reconciliation/recurring_need_reconciliation_service.py`
(rejeu sûr de LA MÊME confirmation grâce à la garde PostgreSQL). Deux passages qui se
chevauchent ne créent jamais de doublon (verrou de ligne + rejeu du résultat stocké)."""

from __future__ import annotations

import logging

from ladini.api.celery_app import celery_app
from ladini.workers.runtime import run_async

logger = logging.getLogger("Ladini.Workers.Cron.RecurringNeedReconciliation")


async def _run() -> dict:
    from ladini.services.reconciliation.recurring_need_reconciliation_service import (
        find_in_doubt_candidates,
        reconcile,
    )

    candidates = await find_in_doubt_candidates()
    outcomes: dict = {}
    for draft, conversation_id in candidates:
        result = await reconcile(draft, conversation_id)
        outcomes[draft.draft_id] = result.outcome.value
    return {"candidates_found": len(candidates), "outcomes": outcomes}


@celery_app.task(name="workers.recurring_need_reconciliation", bind=True, max_retries=1)
def run_recurring_need_reconciliation_cron(self) -> dict:
    try:
        summary: dict = run_async(_run())
        return summary
    except Exception as exc:
        logger.exception("Cron recurring_need_reconciliation en échec")
        raise self.retry(exc=exc, countdown=30) from exc
