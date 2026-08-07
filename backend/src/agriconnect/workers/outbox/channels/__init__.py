"""Canaux de notification — un adaptateur par média, derrière une interface commune."""
from agriconnect.workers.outbox.channels.base import (
    NotificationChannel,
    SendResult,
)
from agriconnect.workers.outbox.channels.whatsapp import WhatsAppChannel
from agriconnect.workers.outbox.channels.email import EmailChannel
from agriconnect.workers.outbox.channels.push import PushChannel

__all__ = [
    "NotificationChannel",
    "SendResult",
    "WhatsAppChannel",
    "EmailChannel",
    "PushChannel",
    "build_channel_registry",
]


def build_channel_registry() -> dict:
    """Registre canal → adaptateur. WhatsApp réel ; Email/Push en interface."""
    return {
        "WHATSAPP": WhatsAppChannel(),
        "EMAIL": EmailChannel(),
        "PUSH": PushChannel(),
    }
