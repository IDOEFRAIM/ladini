"""Cron : réconciliation périodique des `SalesPublishDraft` bloqués en
`EXECUTING` (2026-09-04, migration SALES).

Même structure que `procurement_reconciliation.py`/`preorder_reconciliation.py`
— le service est déjà défensif/idempotent
(`services/reconciliation/sales_publish_reconciliation_service.py`), ce
cron n'ajoute AUCUNE logique métier, seulement l'orchestration périodique."""

from __future__ import annotations

import logging

from ladini.api.celery_app import celery_app
from ladini.workers.runtime import run_async

logger = logging.getLogger("Ladini.Workers.Cron.SalesPublishReconciliation")


async def _run() -> dict:
    from ladini.services.reconciliation.sales_publish_reconciliation_service import (
        find_stale_executing_candidates,
        reconcile_draft,
    )

    outcomes: dict = {}
    candidates = await find_stale_executing_candidates()
    for draft in candidates:
        result = await reconcile_draft(draft)
        outcomes[draft.draft_id] = result.outcome.value
        logger.info(
            "SALES_PUBLISH_RECONCILIATION_CRON | draft_id=%s | outcome=%s | final_status=%s",
            draft.draft_id,
            result.outcome.value,
            result.final_status,
        )

    return {"candidates_found": len(candidates), "outcomes": outcomes}


@celery_app.task(name="workers.sales_publish_reconciliation", bind=True, max_retries=1)
def run_sales_publish_reconciliation_cron(self) -> dict:
    try:
        return run_async(_run())
    except Exception as exc:
        logger.exception("Cron sales_publish_reconciliation en échec")
        raise self.retry(exc=exc, countdown=30) from exc
