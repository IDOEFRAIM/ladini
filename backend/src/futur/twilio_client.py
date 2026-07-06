import json
import logging

from twilio.rest import Client

from agriconnect.core.settings import settings

logger = logging.getLogger("AgriConnect.Services.Twilio")


def send_whatsapp_template(to_number: str, content_sid: str, variables: dict):
    sid = str(settings.TWILIO_ACCOUNT_SID or "").strip()
    token = str(settings.TWILIO_AUTH_TOKEN or "").strip()
    from_number = str(settings.TWILIO_WHATSAPP_NUMBER or "").strip()
    if not sid or not token or not from_number:
        raise RuntimeError("Twilio configuration incomplete")

    client = Client(sid, token)

    try:
        message = client.messages.create(
            from_=from_number,
            to=f"whatsapp:{to_number}",
            content_sid=content_sid,
            content_variables=json.dumps(variables),
        )
        logger.info("WhatsApp template sent sid=%s to=%s", message.sid, to_number)
        return message.sid
    except Exception as e:
        logger.error("Twilio template send failed: %s", e)
        raise
