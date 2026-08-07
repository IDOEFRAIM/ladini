"""Client HTTP Paydunya — génération de facture (Checkout Invoice) et
re-confirmation serveur-à-serveur du statut de paiement.

RÈGLE DE SÉCURITÉ CENTRALE (anti-fraude) : l'IPN reçu sur notre webhook n'est
JAMAIS la source de vérité — n'importe qui peut poster un JSON qui ressemble à
un IPN Paydunya sur notre endpoint. La seule confirmation fiable est un appel
serveur-à-serveur `confirm_invoice()` vers l'API Paydunya elle-même, avec nos
propres clés secrètes. Le webhook (`api/routes/paydunya_webhook.py`) DOIT
toujours rappeler `confirm_invoice(token)` avant de toucher la base — jamais
faire confiance au corps de la requête entrante.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from agriconnect.core.settings import settings

logger = logging.getLogger("agriconnect.services.payments.paydunya")

_BASE_URL = "https://app.paydunya.com/api/v1"
_TIMEOUT_S = 15.0


class PaydunyaError(Exception):
    """Erreur de communication ou de réponse de l'API Paydunya."""

    def __init__(self, message: str, *, response_code: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.response_code = response_code


class PaydunyaClient:
    """Client minimal — deux opérations : créer une facture, confirmer son statut."""

    def __init__(
        self,
        master_key: Optional[str] = None,
        private_key: Optional[str] = None,
        public_key: Optional[str] = None,
        token: Optional[str] = None,
    ) -> None: 
        self._master_key = master_key or settings.PAYDUNYA_MASTER_KEY
        self._private_key = private_key or settings.PAYDUNYA_PRIVATE_KEY
        self._public_key = public_key or settings.PAYDUNYA_PUBLIC_KEY
        self._token = token or settings.PAYDUNYA_TOKEN

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "PAYDUNYA-MASTER-KEY": self._master_key,
            "PAYDUNYA-PRIVATE-KEY": self._private_key,
            "PAYDUNYA-PUBLIC-KEY": self._public_key,
            "PAYDUNYA-TOKEN": self._token,
        }

    async def create_invoice(
        self,
        *,
        order_id: str,
        amount: float,
        description: str,
        callback_url: str,
        store_name: str = "LADINI",
        custom_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Crée une facture Paydunya. Retourne ``{"invoice_token": str, "checkout_url": str}``.

        Lève ``PaydunyaError`` sur tout échec (réseau, clés invalides, montant
        rejeté) — l'appelant ne doit jamais supposer un succès implicite.
        """
        payload = {
            "invoice": {
                "total_amount": round(float(amount), 2),
                "description": description,
            },
            "store": {"name": store_name},
            "actions": {"callback_url": callback_url},
            "custom_data": {"order_id": str(order_id), **(custom_data or {})},
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                resp = await client.post(
                    f"{_BASE_URL}/checkout-invoice/create",
                    json=payload,
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            logger.error("PAYDUNYA_CREATE_INVOICE_HTTP_ERROR | order_id=%s | %s", order_id, exc)
            raise PaydunyaError("Impossible de contacter le service de paiement pour le moment.") from exc

        response_code = str(data.get("response_code") or "")
        if response_code != "00":
            logger.warning(
                "PAYDUNYA_CREATE_INVOICE_REJECTED | order_id=%s | code=%s | msg=%s",
                order_id, response_code, data.get("response_text"),
            )
            raise PaydunyaError(
                data.get("response_text") or "La création de la facture de paiement a échoué.",
                response_code=response_code,
            )

        token = data.get("token")
        checkout_url = data.get("response_text")
        if not token or not checkout_url:
            raise PaydunyaError("Réponse Paydunya incomplète (token/lien de paiement manquant).")

        return {"invoice_token": str(token), "checkout_url": str(checkout_url)}

    async def confirm_invoice(self, invoice_token: str) -> Dict[str, Any]:
        """Re-confirme le statut réel d'une facture DIRECTEMENT auprès de Paydunya.

        Retourne ``{"status": "completed"|"pending"|"cancelled", "amount": float,
        "custom_data": dict}``. Ne fait JAMAIS confiance à un statut fourni par
        l'appelant (ex: le corps brut de l'IPN) — seule cette confirmation
        serveur-à-serveur fait foi.
        """
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                resp = await client.get(
                    f"{_BASE_URL}/checkout-invoice/confirm/{invoice_token}",
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            logger.error("PAYDUNYA_CONFIRM_INVOICE_HTTP_ERROR | token=%s | %s", invoice_token, exc)
            raise PaydunyaError("Impossible de vérifier le statut du paiement pour le moment.") from exc

        raw_status = str(data.get("status") or "").lower()
        invoice = data.get("invoice") or {}
        try:
            amount = float(invoice.get("total_amount") or 0.0)
        except (TypeError, ValueError):
            amount = 0.0

        return {
            "status": raw_status,
            "amount": amount,
            "custom_data": data.get("custom_data") or {},
        }


__all__ = ["PaydunyaClient", "PaydunyaError"]
