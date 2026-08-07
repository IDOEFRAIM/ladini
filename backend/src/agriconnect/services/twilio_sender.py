# agriconnect/services/twilio_sender.py (ou dans vos tasks Celery)

import logging
from twilio.rest import Client
from agriconnect.core.settings import settings

logger = logging.getLogger("AgriConnect.TwilioSender")

MAX_WHATSAPP_BODY_LENGTH = 1200  # Seuil de sécurité pour éviter le blocage Meta/WhatsApp


def send_whatsapp_message(to_phone: str, body_text: str):
    """Envoie un message WhatsApp via Twilio en découpant le texte si nécessaire."""
    if not body_text:
        return

    client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    
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
            body_text[i:i + MAX_WHATSAPP_BODY_LENGTH]
            for i in range(0, len(body_text), MAX_WHATSAPP_BODY_LENGTH)
        ]

    responses = []
    for index, chunk in enumerate(chunks):
        try:
            res = client.messages.create(
                from_=from_formatted,
                to=to_formatted,
                body=chunk
            )
            responses.append(res)
            logger.info(
                "TWILIO_SEND_CHUNK_SUCCESS | chunk=%d/%d | sid=%s | to=%s",
                index + 1, len(chunks), res.sid, to_formatted
            )
        except Exception as e:
            logger.error(
                "TWILIO_SEND_CHUNK_FAILED | chunk=%d/%d | to=%s | error=%s",
                index + 1, len(chunks), to_formatted, e
            )
            
    return responses