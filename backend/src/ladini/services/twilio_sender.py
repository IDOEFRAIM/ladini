# ladini/services/twilio_sender.py (ou dans vos tasks Celery)

import logging
from typing import Optional

from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client

from ladini.core.settings import settings

logger = logging.getLogger("Ladini.TwilioSender")

MAX_WHATSAPP_BODY_LENGTH = (
    1200  # Seuil de sécurité pour éviter le blocage Meta/WhatsApp
)

# ⚠️ RÉSILIENCE — le SDK Twilio construit par défaut un `TwilioHttpClient(
# timeout=None)`, c'est-à-dire AUCUN timeout : une connexion suspendue côté
# Twilio bloquait le thread appelant indéfiniment. Ce sender est invoqué
# depuis les tâches Celery : un seul appel figé immobilisait un worker pour
# toujours, et les messages suivants s'empilaient sans jamais partir.
# Même valeur que les autres clients externes du projet
# (`services/payments/paydunya_client.py`, `services/whatsapp/cloud_api_client.py`).
_TIMEOUT_S = 15.0


def _mask_phone(phone: str) -> str:
    """Numéro tronqué pour les logs (PII) — 4 derniers chiffres conservés."""
    raw = str(phone or "")
    return f"***{raw[-4:]}" if len(raw) >= 4 else "***"


def send_whatsapp_message(to_phone: str, body_text: str):
    """Envoie un message WhatsApp via Twilio en découpant le texte si nécessaire."""
    if not body_text:
        return

    client = Client(
        settings.TWILIO_ACCOUNT_SID,
        settings.TWILIO_AUTH_TOKEN,
        http_client=TwilioHttpClient(timeout=_TIMEOUT_S),
    )

    # Formatage propre des numéros
    clean_from = settings.TWILIO_WHATSAPP_NUMBER.replace("whatsapp:", "").strip()
    clean_to = to_phone.replace("whatsapp:", "").strip()

    from_formatted = f"whatsapp:{clean_from}"
    to_formatted = f"whatsapp:{clean_to}"

    # Découpage du texte s'il dépasse 1200 caractères
    if len(body_text) <= MAX_WHATSAPP_BODY_LENGTH:
        chunks = [body_text]
    else:
        chunks = [
            body_text[i : i + MAX_WHATSAPP_BODY_LENGTH]
            for i in range(0, len(body_text), MAX_WHATSAPP_BODY_LENGTH)
        ]

    responses = []
    for index, chunk in enumerate(chunks):
        try:
            res = client.messages.create(
                from_=from_formatted, to=to_formatted, body=chunk
            )
            responses.append(res)
            logger.info(
                "TWILIO_SEND_CHUNK_SUCCESS | chunk=%d/%d | sid=%s | to=%s",
                index + 1,
                len(chunks),
                res.sid,
                _mask_phone(clean_to),
            )
        except Exception as e:
            logger.error(
                "TWILIO_SEND_CHUNK_FAILED | chunk=%d/%d | to=%s | error=%s",
                index + 1,
                len(chunks),
                _mask_phone(clean_to),
                e,
            )

    return responses


def send_whatsapp_media(
    to_phone: str, media_url: str, caption: str = ""
) -> Optional[str]:
    """Envoie UNE image WhatsApp via Twilio (``media_url``) — renvoie le SID
    Twilio, ou ``None`` en cas d'échec. Une image par appel : WhatsApp
    n'affiche de façon fiable qu'un seul média par message, contrairement à
    SMS/MMS où Twilio accepte une liste."""
    if not media_url:
        return None

    client = Client(
        settings.TWILIO_ACCOUNT_SID,
        settings.TWILIO_AUTH_TOKEN,
        http_client=TwilioHttpClient(timeout=_TIMEOUT_S),
    )

    clean_from = settings.TWILIO_WHATSAPP_NUMBER.replace("whatsapp:", "").strip()
    clean_to = to_phone.replace("whatsapp:", "").strip()

    try:
        res = client.messages.create(
            from_=f"whatsapp:{clean_from}",
            to=f"whatsapp:{clean_to}",
            media_url=[media_url],
            body=caption or "",
        )
        logger.info(
            "TWILIO_SEND_MEDIA_SUCCESS | sid=%s | to=%s",
            res.sid,
            _mask_phone(clean_to),
        )
        return res.sid
    except Exception as e:
        logger.error(
            "TWILIO_SEND_MEDIA_FAILED | to=%s | error=%s",
            _mask_phone(clean_to),
            e,
        )
        return None
