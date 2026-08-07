"""Canal Push — stub derrière l'interface (activable en v2 : FCM/APNs…)."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from agriconnect.workers.outbox.channels.base import SendResult

logger = logging.getLogger("AgriConnect.Workers.Channel.Push")


class PushChannel:
    name = "PUSH"

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
        logger.info("PushChannel non configuré — message ignoré (stub).")
        return SendResult.failure("push_channel_not_implemented")
