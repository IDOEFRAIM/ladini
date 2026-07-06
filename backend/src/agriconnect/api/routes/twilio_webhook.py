"""Twilio WhatsApp Webhook dispatch brut vers l'Orchestrator unique.

Le webhook ne route plus : il transmet (phone, text) à l'Orchestrator via
Celery. C'est le WorkspaceResolver qui décide l'agent, de façon collante.
"""
import logging

from fastapi import APIRouter, Form, HTTPException

from agriconnect.api.tasks import process_agent_task
from agriconnect.graphs.roles import normalize_role
from agriconnect.workspace.store import WorkspaceStore

router = APIRouter()
logger = logging.getLogger("AgriConnect.TwilioWebhook")


@router.post("/webhook/twilio")
async def twilio_webhook(
    From: str = Form(...),
    Body: str = Form(...),
):
    raw_sender = From or ""
    phone = raw_sender.replace("whatsapp:", "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Numéro d'expéditeur invalide.")

    text = (Body or "").strip()

    store = WorkspaceStore()
    workspace = await store.get(phone)

    ws_type = workspace.workspace_type if workspace else None
    resolved_role = None
    force_role = False
    if workspace:
        ws_type = ws_type or "producer"
        resolved_role = normalize_role("BUYER" if ws_type == "buyer" else "PRODUCER")
        force_role = True

    logger.info("Twilio webhook | phone=%s | text=%r | existing_workspace=%s", phone, text[:80], bool(workspace))

    process_agent_task.delay(
        phone_number=phone,
        user_query=text,
        workspace_type=ws_type,
        role=resolved_role,
        force_role=force_role,
    )

    return {"status": "accepted"}