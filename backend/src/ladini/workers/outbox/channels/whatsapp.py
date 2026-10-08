"""Canal WhatsApp (Outbox) — API Cloud Meta par défaut, repli Twilio.

Bascule sur ``settings.MESSAGING_PROVIDER`` (voir ``core/settings.py``) —
même flag que ``api/tasks.py`` pour le chemin conversationnel principal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
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


#: Corps d'un template WhatsApp : 1024 caractères au total, dont ~45 d'enrobage (« Bonjour: … Merci pour votre
#: confiance. ») — la variable `{{1}}` reste donc sous 900.
_TEMPLATE_VAR_LIMIT = 900


def flatten_for_template(text: str) -> str:
    """Rend `text` acceptable comme VARIABLE de template WhatsApp : ni retour à la ligne, ni tabulation, ni plus de 4
    espaces consécutifs (rejetés par Meta). Les sauts de ligne deviennent « | »."""
    flat = re.sub(r"\s*[\r\n]+\s*", " | ", (text or "").strip())
    flat = flat.replace("\t", " ")
    flat = re.sub(r" {2,}", " ", flat)
    flat = re.sub(r"(?:\| ){2,}", "| ", flat)
    return flat.strip(" |")


def template_chunks(body: str, limit: int = _TEMPLATE_VAR_LIMIT) -> List[str]:
    """Texte aplati, coupé sur une frontière d'espace en morceaux <= `limit` (4 messages au plus, comme `_chunk`)."""
    flat = flatten_for_template(body)
    if not flat:
        return [""]
    chunks: List[str] = []
    remaining = flat
    while len(remaining) > limit:
        cut = remaining.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
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

        campaign = bool((payload or {}).get("campaign_recipient_id"))
        window_open = bool((payload or {}).get("service_window_open"))
        if self._provider() == "twilio":
            if campaign and not str(getattr(settings, "TWILIO_PROACTIVE_TEMPLATE_CONTENT_SID", "") or "").strip() and not window_open:
                return SendResult.skip("template_required")
            try:
                # Argument de repli ajouté SEULEMENT pour les campagnes : le chemin historique garde sa signature.
                twilio_args = (recipient_phone, body, False) if campaign else (recipient_phone, body)
                return await asyncio.to_thread(self._send_sync_twilio, *twilio_args)
            except Exception as exc:  # pragma: no cover - dépend du réseau
                logger.warning(
                    "Envoi WhatsApp (Twilio) échoué vers %s : %s", recipient_phone, exc
                )
                return SendResult.failure(str(exc))

        if campaign:
            # Message PROACTIF de campagne : modèle approuvé si configuré ; sinon texte libre UNIQUEMENT dans la
            # fenêtre de service de 24 h ; sinon refus explicite. Jamais de repli sur du libre hors fenêtre.
            template_name = str(getattr(settings, "WHATSAPP_CAMPAIGN_TEMPLATE_NAME", "") or "").strip()
            if template_name:
                try:
                    from ladini.services.whatsapp import cloud_api_client as wa_t

                    ref = await wa_t.send_template(
                        recipient_phone, template_name,
                        str(getattr(settings, "WHATSAPP_CAMPAIGN_TEMPLATE_LANGUAGE", "fr") or "fr"),
                        [template_chunks(body)[0]],
                    )
                    return SendResult.success(provider_ref=ref) if ref else SendResult.failure("send_failed")
                except Exception as exc:  # pragma: no cover - dépend du réseau
                    logger.warning("Envoi modèle WhatsApp échoué vers %s : %s", recipient_phone[-4:], exc)
                    return SendResult.failure(str(exc))
            if not window_open:
                return SendResult.skip("template_required")

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

    def _send_sync_twilio(self, phone: str, body: str, allow_freeform_fallback: bool = True) -> SendResult:
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

        # Message PROACTIF (relance, confirmation de commande, notification) : par template approuvé quand il est
        # configuré — un message libre hors fenêtre de 24 h est refusé par WhatsApp et le message « meurt ». Si le
        # template est refusé (non approuvé, SID invalide), on retombe sur l'envoi libre plutôt que de tout perdre.
        content_sid = str(getattr(settings, "TWILIO_PROACTIVE_TEMPLATE_CONTENT_SID", "") or "").strip()
        if content_sid:
            try:
                for piece in template_chunks(body):
                    msg = client.messages.create(
                        from_=from_number,
                        to=f"whatsapp:{phone}",
                        content_sid=content_sid,
                        content_variables=json.dumps({"1": piece}, ensure_ascii=False),
                    )
                    last_sid = msg.sid
                return SendResult.success(provider_ref=last_sid)
            except Exception as exc:  # noqa: BLE001 - jamais perdre le message : repli libre ci-dessous
                if last_sid is not None:
                    # Un morceau est déjà parti : renvoyer en libre dupliquerait le début du message.
                    logger.warning("TEMPLATE_PARTIAL_SEND | to=%s | %s", phone[-4:], exc)
                    return SendResult.failure(str(exc))
                if not allow_freeform_fallback:
                    # Campagne : jamais de repli sur du texte libre (refusé hors fenêtre de 24 h, et interdit ici).
                    return SendResult.failure(str(exc))
                logger.warning("TEMPLATE_SEND_FAILED_FALLBACK_FREEFORM | to=%s | %s", phone[-4:], exc)

        for chunk in _chunk(body):
            msg = client.messages.create(
                from_=from_number,
                to=f"whatsapp:{phone}",
                body=chunk,
            )
            last_sid = msg.sid
        return SendResult.success(provider_ref=last_sid)
