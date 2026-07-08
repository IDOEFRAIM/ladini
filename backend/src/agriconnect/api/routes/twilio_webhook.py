"""Twilio WhatsApp Webhook dispatch brut vers l'Orchestrator unique.

Le webhook ne route plus : il transmet (phone, text) à l'Orchestrator via
Celery. C'est le WorkspaceResolver qui décide l'agent, de façon collante.
"""
import logging
import redis # ◄ N'oublie pas : pip install redis

from fastapi import APIRouter, Form, HTTPException, Depends, BackgroundTasks
from twilio.rest import Client

from agriconnect.api.tasks import process_agent_task
from agriconnect.graphs.roles import normalize_role
from agriconnect.workspace.store import WorkspaceStore
from agriconnect.api.security import verify_twilio_signature
# Assure-toi d'importer tes settings (à adapter selon ton projet)
from agriconnect.core.settings import settings 

router = APIRouter()
logger = logging.getLogger("AgriConnect.TwilioWebhook")

# 1. Initialisation de Redis (à configurer avec tes variables d'environnement en prod)
# decode_responses=True permet d'obtenir des strings au lieu de bytes
redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)


def send_wait_message(phone_number: str):
    """Fonction exécutée en arrière-plan pour envoyer l'accusé de réception."""
    try:
        # Vérification de sécurité pour éviter de crasher si les variables sont vides
        if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
            return
            
        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        client.messages.create(
            from_=settings.TWILIO_WHATSAPP_NUMBER,
            to=f"whatsapp:{phone_number}",
            body="🌾 *LADINI* : Je suis en train de preparer la reponse. Pourrais tu attendre quelques secondes..."
        )
    except Exception as e:
        logger.error("Impossible d'envoyer l'accusé de réception : %s", e)


@router.post("/webhook/twilio")
async def twilio_webhook(
    background_tasks: BackgroundTasks, # ◄ FastAPI gère ça automatiquement
    From: str = Form(...),
    Body: str = Form(...),
    MessageSid: str = Form(...),       # ◄ Identifiant unique du message Twilio
):
    # --- 1. MODIFIER : ANTI-DOUBLON (Idempotence avec Redis) ---
    redis_key = f"msg:{MessageSid}"
    if redis_client.get(redis_key):
        logger.warning("Message dupliqué détecté : %s. Ignoré.", MessageSid)
        return {"status": "already_processed"}
    
    # On marque le message comme "en cours" pendant 1 heure (3600 secondes)
    redis_client.setex(redis_key, 3600, "processing")
    # -----------------------------------------------------------

    raw_sender = From 
    phone = raw_sender.replace("whatsapp:", "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Numéro d'expéditeur invalide.")

    text = Body.strip()

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

    # Délégation de la tâche IA à Celery
    process_agent_task.delay(
        phone_number=phone,
        user_query=text,
        workspace_type=ws_type,
        role=resolved_role,
        force_role=force_role,
    )

    # --- 2. AJOUTER : Message d'attente immédiat via BackgroundTasks ---
    # Cette tâche s'exécutera juste après que FastAPI ait renvoyé le "200 OK" à Twilio.
    # L'agriculteur reçoit le SMS sans que le webhook ne soit ralenti.
    background_tasks.add_task(send_wait_message, phone)

    return {"status": "accepted"}