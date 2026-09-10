"""Canaux de notification — un adaptateur par média, derrière une interface commune."""

from ladini.workers.outbox.channels.base import (
    NotificationChannel,
    SendResult,
)
from ladini.workers.outbox.channels.email import EmailChannel
from ladini.workers.outbox.channels.push import PushChannel
from ladini.workers.outbox.channels.whatsapp import WhatsAppChannel

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
