import logging

from fastapi import APIRouter, Form

from agriconnect.api.tasks import process_agent_task

router = APIRouter()
logger = logging.getLogger("AgriConnect.TwilioWebhook")


def _hexdump(value: str) -> str:
    return " ".join(f"{ord(ch):02x}" for ch in value) if value else ""


@router.post("/webhook/twilio")
async def twilio_webhook(From: str = Form(...), Body: str = Form(...)):
    raw_sender = From or ""
    phone = raw_sender.replace("whatsapp:", "").strip()
    logger.info(
        "Twilio webhook received | phone=%s | hex=%s | raw=%r",
        phone,
        _hexdump(phone),
        raw_sender,
    )

    process_agent_task.delay(
        role="PRODUCER",
        phone_number=phone,
        user_query=Body,
    )

    return {"status": "accepted"}