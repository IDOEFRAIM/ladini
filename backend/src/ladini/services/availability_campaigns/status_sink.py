"""Reçoit les statuts de livraison fournisseur (Meta / Twilio) et les applique aux destinataires de campagne.

Best-effort : un échec ici ne doit JAMAIS casser le webhook (les statuts sont informatifs ; le fournisseur peut les
renvoyer). Les transitions sont monotones (voir `campaign_service.record_delivery_status`), donc un rejeu ou un
désordre d'arrivée est sans effet.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("Ladini.Campaigns.StatusSink")

_TWILIO_TO_CANONICAL = {"delivered": "delivered", "read": "read", "failed": "failed", "undelivered": "failed"}


async def apply_provider_status(provider_ref: Optional[str], status: str, *, error: Optional[str] = None) -> bool:
    ref = str(provider_ref or "").strip()
    st = str(status or "").strip().lower()
    st = _TWILIO_TO_CANONICAL.get(st, st)
    if not ref or st not in {"delivered", "read", "failed"}:
        return False
    try:
        from ladini.services.availability_campaigns import campaign_service
        from ladini.workers.runtime import worker_session

        async with worker_session() as session:
            return bool(await campaign_service.record_delivery_status(session, ref, st, error=error))
    except Exception:  # noqa: BLE001 - jamais bloquant pour le webhook
        logger.warning("CAMPAIGN_STATUS_SINK_FAILED | status=%s", st, exc_info=True)
        return False
