import asyncio
import logging
from typing import List, Optional

from twilio.rest import Client

from agriconnect.api.celery_app import celery_app
from agriconnect.core.database import close_db
from agriconnect.core.settings import settings
from agriconnect.orchestrator import Orchestrator

logger = logging.getLogger("AgriConnect.Worker")

_orchestrator = Orchestrator()

_TWILIO_SOFT_LIMIT = 1500
_TWILIO_DISCLAIMER = " (Détails complets disponibles sur votre dashboard)"


def _chunk_whatsapp_body(body: str, limit: int = _TWILIO_SOFT_LIMIT) -> List[str]:
    """Split outgoing message into Twilio-compliant chunks (1600 chars).

    We keep a safety margin (default 1500) and prefer newline boundaries.
    When text is too long even after chunking, we append a dashboard disclaimer.
    """

    if not body:
        return [""]

    remaining = body.strip()
    chunks: List[str] = []

    while len(remaining) > limit:
        split_idx = remaining.rfind("\n", 0, limit)
        if split_idx == -1 or split_idx < limit // 2:
            split_idx = limit
        chunk = remaining[:split_idx].rstrip()
        chunks.append(chunk)
        remaining = remaining[split_idx:].lstrip()

    if remaining:
        chunks.append(remaining)

    if len(chunks) > 4:
        kept = chunks[:3]
        kept.append(f"{chunks[3][:limit - len(_TWILIO_DISCLAIMER) - 5]} {_TWILIO_DISCLAIMER}")
        logger.warning("Response exceeded chunk limit; truncated with disclaimer")
        return kept

    return chunks


@celery_app.task(
    bind=True,
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=5,
    retry_jitter=True,
)
def process_agent_task(
    self,
    phone_number: str = "",
    user_query: str = "",
    workspace_type: Optional[str] = None,
    role: Optional[str] = None,
    force_role: bool = False,
):
    """Point d'entrée worker : délègue tout à l'Orchestrator unique."""

    async def _run():
        try:
            resolved_type = workspace_type
            forced = bool(force_role)
            if not resolved_type and role:
                resolved_type = "buyer" if role.upper() == "BUYER" else "producer"
                forced = True
            return await _orchestrator.handle(
                phone_number,
                user_query,
                workspace_type=resolved_type,
                force_role=forced,
            )
        finally:
            try:
                await close_db()
            except Exception as exc:
                logger.warning("close_db failed: %s", exc)

    try:
        result = asyncio.run(_run())
    except Exception as e:
        logger.error("Erreur orchestrateur: %s", e)
        raise

    final_text = result.get("final_response", "Je n'ai pas pu générer de réponse.")
    account_sid = str(settings.TWILIO_ACCOUNT_SID or "").strip()
    auth_token = str(settings.TWILIO_AUTH_TOKEN or "").strip()
    from_number = str(settings.TWILIO_WHATSAPP_NUMBER or "").strip()
    if not account_sid or not auth_token or not from_number:
        logger.error("Twilio configuration incomplete; cannot send WhatsApp response")
        raise RuntimeError("Twilio configuration incomplete")

    client = Client(account_sid, auth_token)
    chunks = _chunk_whatsapp_body(str(final_text))

    try:
        last_sid = None
        for chunk in chunks:
            message = client.messages.create(
                from_=from_number,
                to=f"whatsapp:{phone_number}",
                body=chunk,
            )
            last_sid = message.sid
        return {"status": "message_sent", "sid": last_sid, "chunks": len(chunks)}
    except Exception as e:
        logger.error("Erreur Twilio : %s", e)
        raise
