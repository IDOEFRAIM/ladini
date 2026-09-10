"""Canal WhatsApp (Outbox) — API Cloud Meta par défaut, repli Twilio.

Bascule sur ``settings.MESSAGING_PROVIDER`` (voir ``core/settings.py``) —
même flag que ``api/tasks.py`` pour le chemin conversationnel principal.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from ladini.core.settings import settings
from ladini.workers.outbox.channels.base import SendResult

logger = logging.getLogger("Ladini.Workers.Channel.WhatsApp")

_TWILIO_SOFT_LIMIT = 1500
# Aligné sur les autres clients externes (paydunya_client, cloud_api_client,
# twilio_sender) — voir la justification dans `_send_sync_twilio`.
_TWILIO_TIMEOUT_S = 15.0


def _chunk(body: str, limit: int = _TWILIO_SOFT_LIMIT) -> List[str]:
    body = (body or "").strip()
    if not body:
        return [""]
    chunks: List[str] = []
    remaining = body
    while len(remaining) > limit:
        split_idx = remaining.rfind("\n", 0, limit)
        if split_idx == -1 or split_idx < limit // 2:
            split_idx = limit
        chunks.append(remaining[:split_idx].rstrip())
        remaining = remaining[split_idx:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks[:4]


class WhatsAppChannel:
    name = "WHATSAPP"

    def _provider(self) -> str:
        return (
            str(getattr(settings, "MESSAGING_PROVIDER", "") or "whatsapp_cloud")
            .strip()
            .lower()
        )

    def is_configured(self) -> bool:
        if self._provider() == "twilio":
            return bool(
                str(settings.TWILIO_ACCOUNT_SID or "").strip()
                and str(settings.TWILIO_AUTH_TOKEN or "").strip()
                and str(settings.TWILIO_WHATSAPP_NUMBER or "").strip()
            )
        from ladini.services.whatsapp import cloud_api_client as wa

        return wa.is_configured()

    async def send(
        self,
        *,
        body: str,
        recipient_phone: Optional[str] = None,
        recipient_user_id: Optional[Any] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self.is_configured():
            return SendResult.failure("whatsapp_not_configured")
        if not recipient_phone:
            return SendResult.failure("missing_recipient_phone")

        if self._provider() == "twilio":
            try:
                return await asyncio.to_thread(
                    self._send_sync_twilio, recipient_phone, body
                )
            except Exception as exc:  # pragma: no cover - dépend du réseau
                logger.warning(
                    "Envoi WhatsApp (Twilio) échoué vers %s : %s", recipient_phone, exc
                )
                return SendResult.failure(str(exc))

        try:
            from ladini.services.whatsapp import cloud_api_client as wa

            message_ids = await wa.send_text(recipient_phone, body)
            if not message_ids:
                return SendResult.failure("send_failed")
            return SendResult.success(provider_ref=message_ids[-1])
        except Exception as exc:  # pragma: no cover - dépend du réseau
            logger.warning(
                "Envoi WhatsApp (Cloud API) échoué vers %s : %s", recipient_phone, exc
            )
            return SendResult.failure(str(exc))

    def _send_sync_twilio(self, phone: str, body: str) -> SendResult:
        from twilio.http.http_client import TwilioHttpClient
        from twilio.rest import Client

        # Timeout explicite : le SDK Twilio n'en pose AUCUN par défaut
        # (`TwilioHttpClient(timeout=None)`). Ici l'appel tourne dans un thread
        # via `asyncio.to_thread` depuis le cron outbox — sans timeout, une
        # connexion suspendue immobilise un thread du pool ET fige le
        # dispatcher, bloquant toute la file de notifications derrière lui.
        client = Client(
            str(settings.TWILIO_ACCOUNT_SID).strip(),
            str(settings.TWILIO_AUTH_TOKEN).strip(),
            http_client=TwilioHttpClient(timeout=_TWILIO_TIMEOUT_S),
        )
        from_number = str(settings.TWILIO_WHATSAPP_NUMBER).strip()
        last_sid: Optional[str] = None
        for chunk in _chunk(body):
            msg = client.messages.create(
                from_=from_number,
                to=f"whatsapp:{phone}",
                body=chunk,
            )
            last_sid = msg.sid
        return SendResult.success(provider_ref=last_sid)
