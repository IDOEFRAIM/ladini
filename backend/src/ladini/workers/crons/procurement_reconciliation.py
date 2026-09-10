"""Cron : réconciliation périodique des `ProcurementDraft` bloqués en
`EXECUTING` (2026-09-03, mandat recovery phase 1/4).

`find_stale_executing_candidates` (seuil configurable,
`settings.PROCUREMENT_EXECUTING_STALE_SECONDS`) → `reconcile_draft` par
candidat — les deux fonctions sont déjà défensives/idempotentes
(`services/reconciliation/procurement_reconciliation_service.py`), ce cron
n'ajoute AUCUNE logique métier, seulement l'orchestration périodique.

Idempotent par construction (comme les autres crons de ce module) :
`reconcile_draft` réutilise le CAS de `procurement_draft_store`, deux
exécutions qui se chevauchent (retard, retry Celery) ne produisent jamais
une double finalisation — voir
`tests/architecture/test_procurement_reconciliation_service.py::
TestReconciliationConcurrency`."""

from __future__ import annotations

import logging

from ladini.api.celery_app import celery_app
from ladini.workers.runtime import run_async

logger = logging.getLogger("Ladini.Workers.Cron.ProcurementReconciliation")


async def _run() -> dict:
    from ladini.services.reconciliation.procurement_reconciliation_service import (
        find_stale_executing_candidates,
        reconcile_draft,
    )

    candidates = await find_stale_executing_candidates()
    outcomes: dict = {}
    for draft in candidates:
        result = await reconcile_draft(draft)
        outcomes[draft.draft_id] = result.outcome.value
        logger.info(
            "PROCUREMENT_RECONCILIATION_CRON | draft_id=%s | outcome=%s | final_status=%s",
            draft.draft_id,
            result.outcome.value,
            result.final_status,
        )
    return {"candidates_found": len(candidates), "outcomes": outcomes}


@celery_app.task(name="workers.procurement_reconciliation", bind=True, max_retries=1)
def run_procurement_reconciliation_cron(self) -> dict:
    try:
        return run_async(_run())
    except Exception as exc:
        logger.exception("Cron procurement_reconciliation en échec")
        raise self.retry(exc=exc, countdown=30) from exc
