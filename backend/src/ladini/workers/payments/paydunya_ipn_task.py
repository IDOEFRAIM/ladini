"""Traitement asynchrone de l'IPN Paydunya — déclenché par le webhook via ``.delay()``.

RÈGLE DE SÉCURITÉ CENTRALE : cette tâche ne reçoit du webhook QUE le
``invoice_token`` brut (aucun champ métier du corps de l'IPN n'est transmis
ni utilisé). Elle re-confirme elle-même le statut réel directement auprès de
Paydunya (``PaydunyaClient.confirm_invoice``) avant toute écriture en base —
voir ``services/payments/paydunya_client.py`` pour le pourquoi.

(2026-09-03, clôture escrow/IPN) : la logique de re-confirmation + synchro
``Order``/``PreorderDraft`` vit désormais dans
``flows/buyer/preorder_payment.py::reconcile_invoice`` — PARTAGÉE avec
``PreorderReconciliationService`` (mandat §26 : ne pas dupliquer cette
branche à deux endroits). Ce module reste le point d'ENTRÉE Celery
(retry/logging), rien de plus.
"""

from __future__ import annotations

import logging

from ladini.api.celery_app import celery_app
from ladini.workers.runtime import run_async

logger = logging.getLogger("Ladini.Workers.Payments.PaydunyaIPN")


async def _run(invoice_token: str) -> dict:
    from ladini.graphs.agents.market_coach.flows.buyer.preorder_payment import (
        reconcile_invoice,
    )

    result = await reconcile_invoice(invoice_token)
    logger.info(
        "PAYDUNYA_IPN_PROCESSED | token=%s | status=%s | order_id=%s | already_processed=%s",
        invoice_token,
        result.get("status"),
        result.get("order_id"),
        result.get("already_processed", False),
    )
    return result


@celery_app.task(
    name="workers.process_paydunya_ipn",
    bind=True,
    max_retries=3,
    default_retry_delay=10,
)
def process_paydunya_ipn(self, invoice_token: str) -> dict:
    try:
        return run_async(_run(invoice_token))
    except Exception as exc:
        logger.exception("PAYDUNYA_IPN_TASK_ERROR | token=%s", invoice_token)
        raise self.retry(exc=exc) from exc
