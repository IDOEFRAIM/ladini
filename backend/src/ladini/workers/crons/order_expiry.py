"""Cron : annule les commandes dont le paiement escrow (Paydunya) a expiré.

Ne touche jamais une commande déjà ESCROWED/PAID_OUT (voir
``EscrowMixin.expire_pending_payments``) — uniquement celles encore
``payment_status == PENDING`` au-delà de ``payment_expires_at``. Comme les
précommandes DRAFT ne débitent pas le stock, "libérer la disponibilité" est
automatique dès l'annulation : rien de plus à faire côté producteur.
"""

from __future__ import annotations

import logging

from ladini.api.celery_app import celery_app
from ladini.workers.runtime import run_async, worker_session

logger = logging.getLogger("Ladini.Workers.Cron.OrderExpiry")


async def _run() -> dict:
    from ladini.services.database.d import AgriDatabaseService

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
        # (2026-09-03, clôture escrow/IPN) : synchronise `PreorderDraft`
        # pour chaque commande expirée — même gap que l'IPN Paydunya
        # (`paydunya_ipn_task.py::_sync_preorder_draft`), même discipline
        # best-effort (une commande hors du tunnel PREORDER n'a pas de
        # draft correspondant, ce n'est jamais une erreur).
        from ladini.graphs.agents.market_coach.flows.buyer.preorder_payment import (
            apply_payment_expiry,
        )

        for order_id in result.get("expired_order_ids") or []:
            try:
                await apply_payment_expiry(str(order_id))
            except Exception:
                logger.exception("ORDER_EXPIRY_DRAFT_SYNC_FAILED | order_id=%s", order_id)
    return result


@celery_app.task(name="workers.order_expiry", bind=True, max_retries=1)
def run_order_expiry_cron(self) -> dict:
    try:
        return run_async(_run())
    except Exception as exc:
        logger.exception("Cron order_expiry en échec")
        raise self.retry(exc=exc, countdown=30) from exc
