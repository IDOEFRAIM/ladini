"""Traitement asynchrone de l'IPN Paydunya — déclenché par le webhook via ``.delay()``.

RÈGLE DE SÉCURITÉ CENTRALE : cette tâche ne reçoit du webhook QUE le
``invoice_token`` brut (aucun champ métier du corps de l'IPN n'est transmis
ni utilisé). Elle re-confirme elle-même le statut réel directement auprès de
Paydunya (``PaydunyaClient.confirm_invoice``) avant toute écriture en base —
voir ``services/payments/paydunya_client.py`` pour le pourquoi.
"""
from __future__ import annotations

import logging

from agriconnect.api.celery_app import celery_app
from agriconnect.workers.runtime import run_async, worker_session

logger = logging.getLogger("AgriConnect.Workers.Payments.PaydunyaIPN")


async def _run(invoice_token: str) -> dict:
    from agriconnect.services.payments.paydunya_client import PaydunyaClient, PaydunyaError
    from agriconnect.services.database.d import AgriDatabaseService

    client = PaydunyaClient()
    try:
        confirmed = await client.confirm_invoice(invoice_token)
    except PaydunyaError as exc:
        logger.error("PAYDUNYA_IPN_CONFIRM_FAILED | token=%s | %s", invoice_token, exc)
        return {"status": "error", "reason": "confirm_failed"}

    if confirmed["status"] != "completed":
        logger.info(
            "PAYDUNYA_IPN_NOT_COMPLETED | token=%s | status=%s",
            invoice_token, confirmed["status"],
        )
        return {"status": "ignored", "paydunya_status": confirmed["status"]}

    # `mark_escrow_paid` lit `self.session` via le ContextVar `db_session_ctx`
    # (pas de décorateur `@transactional`, voir escrow.py) — sans une session
    # ouverte explicitement ici, elle lève TOUJOURS
    # `BusinessRuleException("Session indisponible.")`. Sans ce fix, TOUT
    # paiement Paydunya confirmé échouait à être marqué payé en base (stock
    # jamais débité, OTP jamais généré, aucune notification envoyée) malgré
    # la confirmation réussie côté Paydunya — bug critique côté argent réel.
    async with worker_session():
        result = await AgriDatabaseService().mark_escrow_paid(invoice_token)
    logger.info(
        "PAYDUNYA_IPN_PROCESSED | token=%s | order_id=%s | already_processed=%s",
        invoice_token, result.get("order_id"), result.get("already_processed", False),
    )
    return result


@celery_app.task(name="workers.process_paydunya_ipn", bind=True, max_retries=3, default_retry_delay=10)
def process_paydunya_ipn(self, invoice_token: str) -> dict:
    try:
        return run_async(_run(invoice_token))
    except Exception as exc:
        logger.exception("PAYDUNYA_IPN_TASK_ERROR | token=%s", invoice_token)
        raise self.retry(exc=exc)
