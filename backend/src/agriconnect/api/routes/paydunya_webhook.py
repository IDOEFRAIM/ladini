"""Webhook IPN Paydunya — extraction du token UNIQUEMENT, jamais du contenu métier.

RÈGLE DE SÉCURITÉ CENTRALE (anti-fraude) : le corps de cette requête n'est
JAMAIS une source de vérité — n'importe qui peut poster un JSON qui ressemble
à un IPN Paydunya sur cet endpoint. Ce handler extrait uniquement le
``invoice_token`` (un identifiant, pas une donnée métier) et délègue TOUT le
reste à ``workers.payments.paydunya_ipn_task``, qui re-confirme le statut réel
en rappelant l'API Paydunya avec nos propres clés secrètes avant de toucher
la base. Le webhook lui-même ne lit ni ne fait confiance à ``response_code``,
``status``, ni au montant transmis dans le corps.

Répond toujours vite (< 200ms visé) : aucun travail DB/HTTP ici, tout est
délégué à Celery — même pattern que ``twilio_webhook.py``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Request, Response

router = APIRouter()
logger = logging.getLogger("AgriConnect.PaydunyaWebhook")


def _ok() -> Response:
    # Paydunya n'attend pas de TwiML — un 200 texte simple suffit à
    # acquitter l'IPN et éviter les re-livraisons en boucle côté Paydunya.
    return Response(content="OK", media_type="text/plain")


def _extract_invoice_token(payload: Any) -> Optional[str]:
    """Cherche le token de facture dans les quelques formes connues du corps
    Paydunya, sans jamais interpréter le reste du contenu."""
    if not isinstance(payload, dict):
        return None
    direct = payload.get("token") or payload.get("invoice_token")
    if direct:
        return str(direct).strip()
    invoice = payload.get("invoice")
    if isinstance(invoice, dict) and invoice.get("token"):
        return str(invoice["token"]).strip()
    custom_data = payload.get("custom_data")
    if isinstance(custom_data, dict) and custom_data.get("token"):
        return str(custom_data["token"]).strip()
    return None


@router.post("/webhooks/paydunya-ipn")
async def paydunya_ipn(request: Request) -> Response:
    try:
        return await _handle_paydunya_ipn(request)
    except Exception:
        # Filet de sécurité : ne jamais laisser une erreur interne provoquer
        # un retry en boucle de Paydunya sur un payload qu'on ne peut pas lire.
        logger.exception("PAYDUNYA_WEBHOOK_UNHANDLED_ERROR")
        return _ok()


async def _handle_paydunya_ipn(request: Request) -> Response:
    import json as _json

    payload: Any = None
    content_type = (request.headers.get("content-type") or "").lower()

    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception:
            payload = None
    else:
        form = dict(await request.form())
        raw_data = form.get("data")
        if raw_data:
            try:
                payload = _json.loads(raw_data)
            except (TypeError, ValueError):
                payload = None
        if payload is None:
            payload = form

    invoice_token = _extract_invoice_token(payload)
    if not invoice_token:
        logger.warning(
            "PAYDUNYA_WEBHOOK_NO_TOKEN | keys=%s",
            list(payload.keys()) if isinstance(payload, dict) else type(payload),
        )
        return _ok()

    logger.info("PAYDUNYA_WEBHOOK_RECEIVED | token=%s", invoice_token)

    from agriconnect.workers.payments.paydunya_ipn_task import process_paydunya_ipn

    process_paydunya_ipn.delay(invoice_token)

    return _ok()
