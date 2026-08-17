"""Cron : annule les commandes dont le paiement escrow (Paydunya) a expiré.

Ne touche jamais une commande déjà ESCROWED/PAID_OUT (voir
``EscrowMixin.expire_pending_payments``) — uniquement celles encore
``payment_status == PENDING`` au-delà de ``payment_expires_at``. Comme les
précommandes DRAFT ne débitent pas le stock, "libérer la disponibilité" est
automatique dès l'annulation : rien de plus à faire côté producteur.
"""

from __future__ import annotations

import logging

from agriconnect.api.celery_app import celery_app
from agriconnect.workers.runtime import run_async, worker_session

logger = logging.getLogger("AgriConnect.Workers.Cron.OrderExpiry")


async def _run() -> dict:
    from agriconnect.services.database.d import AgriDatabaseService

    # `expire_pending_payments` lit `self.session` via le ContextVar
    # `db_session_ctx` (pas de décorateur `@transactional`, voir escrow.py) —
    # sans une session ouverte explicitement ici, elle lève TOUJOURS
    # `BusinessRuleException("Session indisponible.")`. Même pattern que
    # `auction_solicitation.py`.
    async with worker_session():
        result = await AgriDatabaseService().expire_pending_payments()
    if result.get("expired_count"):
        logger.info(
            "ORDER_EXPIRY | %d commande(s) annulée(s) : %s",
            result["expired_count"],
            result.get("expired_order_ids"),
        )
    return result


@celery_app.task(name="workers.order_expiry", bind=True, max_retries=1)
def run_order_expiry_cron(self) -> dict:
    try:
        return run_async(_run())
    except Exception as exc:
        logger.exception("Cron order_expiry en échec")
        raise self.retry(exc=exc, countdown=30) from exc
