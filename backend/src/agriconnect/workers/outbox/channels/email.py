"""Canal Email — stub derrière l'interface (activable en v2 : SES/SendGrid…)."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from agriconnect.workers.outbox.channels.base import SendResult

logger = logging.getLogger("AgriConnect.Workers.Channel.Email")


class EmailChannel:
    name = "EMAIL"

    def is_configured(self) -> bool:
        return False  # aucun provider câblé en v1

    async def send(
        self,
        *,
        body: str,
        recipient_phone: Optional[str] = None,
        recipient_user_id: Optional[Any] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        # Interface prête : brancher ici un provider (SES/SendGrid) en v2.
        logger.info("EmailChannel non configuré — message ignoré (stub).")
        return SendResult.failure("email_channel_not_implemented")
