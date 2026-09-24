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
        cancel_abandoned,
        find_abandoned_candidates,
        find_in_doubt_candidates,
        reconcile,
    )

    candidates = await find_in_doubt_candidates()
    outcomes: dict = {}
    for draft, conversation_id in candidates:
        result = await reconcile(draft, conversation_id)
        outcomes[draft.draft_id] = result.outcome.value

    abandoned = await find_abandoned_candidates()
    cancelled = 0
    for draft, _conversation_id in abandoned:
        if await cancel_abandoned(draft):
            cancelled += 1

    return {
        "candidates_found": len(candidates),
        "outcomes": outcomes,
        "abandoned_found": len(abandoned),
        "abandoned_cancelled": cancelled,
    }


@celery_app.task(name="workers.recurring_need_reconciliation", bind=True, max_retries=1)
def run_recurring_need_reconciliation_cron(self) -> dict:
    try:
        summary: dict = run_async(_run())
        return summary
    except Exception as exc:
        logger.exception("Cron recurring_need_reconciliation en échec")
        raise self.retry(exc=exc, countdown=30) from exc
