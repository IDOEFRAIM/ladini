"""
WhatsApp Tasks — Envoi de messages via Twilio (production-grade).

PRINCIPES :
  - Hérite de AgriTask (structured logging, backoff, classification)
  - Twilio importé LAZY (dans la tâche)
  - Rate limit respecté (1 msg/sec via task annotation dans celery_config)
  - Validation du numéro de téléphone avant envoi
  - Idempotency : le message_sid Twilio sert de preuve d'envoi
  - Retry intelligent : 429 → long backoff, 4xx → fatal, 5xx → retry
  - Message tronqué si trop long (WhatsApp limite à 4096 chars)
"""

import logging
import re
from typing import Optional

from celery.exceptions import SoftTimeLimitExceeded

from agriconnect.workers.celery_app import celery_app
from agriconnect.workers.celery_config import TIME_LIMITS
from agriconnect.workers.task_base import (
    AgriTask,
    ExternalServiceDown,
    FatalTaskError,
    RateLimitHit,
    error_result,
    success_result,
)
from agriconnect.core.settings import settings

logger = logging.getLogger("AgriConnect.tasks.whatsapp")

_WA_LIMITS = TIME_LIMITS["whatsapp"]

# WhatsApp message limit
MAX_MESSAGE_LENGTH = 4096

# Regex basique pour valider un numéro international
_PHONE_PATTERN = re.compile(r"^\+?[1-9]\d{7,14}$")


def _validate_phone(number: str) -> str:
    """Valide et normalise un numéro de téléphone."""
    cleaned = re.sub(r"[\s\-\(\)]", "", number)
    if not _PHONE_PATTERN.match(cleaned):
        raise FatalTaskError(f"Invalid phone number format: {number}")
    return cleaned


def _truncate_message(message: str) -> str:
    """Tronque le message si trop long pour WhatsApp."""
    if len(message) > MAX_MESSAGE_LENGTH:
        truncated = message[:MAX_MESSAGE_LENGTH - 30]
        return truncated + "\n\n[Message tronqué]"
    return message


def _get_twilio_config():
    """Retourne la config Twilio ou lève FatalTaskError si manquante."""
    sid = settings.TWILIO_ACCOUNT_SID
    token = settings.TWILIO_AUTH_TOKEN
    from_num = settings.TWILIO_WHATSAPP_NUMBER
    if not all([sid, token, from_num]):
        raise FatalTaskError(
            "Twilio non configuré (SID, TOKEN ou WHATSAPP_NUMBER manquant)"
        )
    return sid, token, from_num


@celery_app.task(
    base=AgriTask,
    name="agriconnect.workers.tasks.whatsapp.send_message",
    bind=True,
    max_retries=3,
    soft_time_limit=_WA_LIMITS["soft"],
    time_limit=_WA_LIMITS["hard"],
    acks_late=True,
    track_started=True,
)
def send_message(
    self,
    to_number: str,
    message: str,
    media_url: Optional[str] = None,
):
    """
    Envoie un message WhatsApp via Twilio.

    Guards:
      - Numéro invalide → FatalTaskError (pas de retry)
      - Twilio non configuré → FatalTaskError
      - Message vide → FatalTaskError
      - Twilio 429 → RateLimitHit → long backoff
      - Twilio 5xx → ExternalServiceDown → retry standard

    Returns:
        dict: {"message_sid", "to", "status", ...}
    """
    try:
        # ── Validation ──
        if not message or not message.strip():
            raise FatalTaskError("Empty message — nothing to send")

        to_number = _validate_phone(to_number)
        message = _truncate_message(message)
        sid, token, from_num = _get_twilio_config()

        # ── Lazy import Twilio ──
        from twilio.rest import Client
        from twilio.base.exceptions import TwilioRestException

        client = Client(sid, token)
        params = {
            "from_": from_num,
            "to": f"whatsapp:{to_number}",
            "body": message,
        }
        if media_url:
            params["media_url"] = [media_url]

        twilio_msg = client.messages.create(**params)

        return success_result(
            data={
                "message_sid": twilio_msg.sid,
                "to": to_number,
                "message_length": len(message),
            },
            task_name=self.name,
        )

    except FatalTaskError:
        raise

    except SoftTimeLimitExceeded:
        return self.handle_timeout({"to": to_number})

    except Exception as exc:
        # Classifier les erreurs Twilio
        exc_str = str(exc).lower()
        status_code = getattr(exc, "status", 0) or getattr(exc, "code", 0)

        if status_code == 429 or "rate limit" in exc_str:
            raise RateLimitHit(f"Twilio rate limit: {exc}") from exc

        if 400 <= (status_code or 0) < 500 and status_code != 429:
            # Erreur client 4xx (sauf 429) → fatal
            return error_result(
                error=f"Twilio client error: {exc}",
                task_name=self.name,
                retryable=False,
            )

        # 5xx ou autre erreur → retryable
        self.retry_with_backoff(exc, base_delay=15.0, max_delay=120.0)


@celery_app.task(
    base=AgriTask,
    name="agriconnect.workers.tasks.whatsapp.send_bulk_messages",
    bind=True,
    max_retries=1,
    soft_time_limit=600,
    time_limit=720,
    acks_late=True,
    track_started=True,
)
def send_bulk_messages(
    self,
    recipients: list,
    message: str,
    media_url: Optional[str] = None,
):
    """
    Envoie un message WhatsApp à plusieurs destinataires.
    Chaque envoi est une sous-tâche indépendante (isolation des erreurs).

    Args:
        recipients: Liste de numéros de téléphone
        message: Le message à envoyer
        media_url: URL média optionnelle
    """
    if not recipients:
        raise FatalTaskError("Empty recipients list")

    results = {"sent": 0, "failed": 0, "errors": []}

    for number in recipients:
        try:
            send_message.delay(
                to_number=number,
                message=message,
                media_url=media_url,
            )
            results["sent"] += 1
        except Exception as e:
            results["failed"] += 1
            results["errors"].append({"number": number, "error": str(e)})
            logger.warning("Failed to queue WhatsApp for %s: %s", number, e)

    return success_result(data=results, task_name=self.name)
