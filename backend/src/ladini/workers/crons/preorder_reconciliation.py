"""Cron : réconciliation périodique des `PreorderDraft` bloqués en
`EXECUTING` ou `AWAITING_PAYMENT` (2026-09-03, clôture escrow/IPN).

Même structure que `procurement_reconciliation.py` — les deux fonctions du
service sont déjà défensives/idempotentes
(`services/reconciliation/preorder_reconciliation_service.py`), ce cron
n'ajoute AUCUNE logique métier, seulement l'orchestration périodique des
DEUX candidats (EXECUTING et AWAITING_PAYMENT)."""

from __future__ import annotations

import logging

from ladini.api.celery_app import celery_app
from ladini.workers.runtime import run_async

logger = logging.getLogger("Ladini.Workers.Cron.PreorderReconciliation")


async def _run() -> dict:
    from ladini.services.reconciliation.preorder_reconciliation_service import (
        find_stale_awaiting_payment_candidates,
        find_stale_executing_candidates,
        reconcile_awaiting_payment_draft,
        reconcile_executing_draft,
    )

    outcomes: dict = {}

    executing_candidates = await find_stale_executing_candidates()
    for draft in executing_candidates:
        result = await reconcile_executing_draft(draft)
        outcomes[draft.draft_id] = result.outcome.value
        logger.info(
            "PREORDER_RECONCILIATION_CRON | phase=EXECUTING | draft_id=%s | outcome=%s | final_status=%s",
            draft.draft_id,
            result.outcome.value,
            result.final_status,
        )

    awaiting_payment_candidates = await find_stale_awaiting_payment_candidates()
    for draft in awaiting_payment_candidates:
        result = await reconcile_awaiting_payment_draft(draft)
        outcomes[draft.draft_id] = result.outcome.value
        logger.info(
            "PREORDER_RECONCILIATION_CRON | phase=AWAITING_PAYMENT | draft_id=%s | outcome=%s | final_status=%s",
            draft.draft_id,
            result.outcome.value,
            result.final_status,
        )

    return {
        "executing_candidates_found": len(executing_candidates),
        "awaiting_payment_candidates_found": len(awaiting_payment_candidates),
        "outcomes": outcomes,
    }


@celery_app.task(name="workers.preorder_reconciliation", bind=True, max_retries=1)
def run_preorder_reconciliation_cron(self) -> dict:
    try:
        return run_async(_run())
    except Exception as exc:
        logger.exception("Cron preorder_reconciliation en échec")
        raise self.retry(exc=exc, countdown=30) from exc
