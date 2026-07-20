"""Twilio WhatsApp Webhook dispatch brut vers l'Orchestrator unique.

Le webhook ne route plus : il transmet (phone, text) à l'Orchestrator via
Celery. C'est le WorkspaceResolver qui décide l'agent, de façon collante.
"""
import logging
from typing import Optional

import redis # ◄ N'oublie pas : pip install redis

from fastapi import APIRouter, Form, HTTPException, Depends, BackgroundTasks, Request
from twilio.rest import Client

from agriconnect.api.tasks import process_agent_task
from agriconnect.graphs.roles import normalize_role
from agriconnect.workspace.store import WorkspaceStore
from agriconnect.api.security import verify_twilio_signature
# Assure-toi d'importer tes settings (à adapter selon ton projet)
from agriconnect.core.settings import settings 

router = APIRouter()
logger = logging.getLogger("AgriConnect.TwilioWebhook")

# 1. Initialisation de Redis — lit settings.REDIS_URL (env var en prod).
# Le défaut de settings.REDIS_URL ("redis://localhost:6379/0") préserve le
# comportement local/dev si la variable n'est pas définie ; en prod, définir
# REDIS_URL (ou VALKEY_ENDPOINT via un futur ajustement) pointe ce client vers
# le vrai Redis managé au lieu d'un localhost qui n'existe pas sur ce process.
redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)


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


def _extract_interactive_id(form: dict) -> Optional[str]:
    """Extrait le payload d'un clic WhatsApp (bouton quick-reply / ligne de liste).

    Les noms de champs Twilio pour les réponses interactives varient selon la
    configuration du compte/sender. On tente les plus courants dans l'ordre :
      - `ButtonPayload` : payload défini sur un bouton quick-reply (le plus fiable)
      - `ListId` / `SelectedId` : id de la ligne de liste choisie
    On NE retombe PAS sur `Body` ici : un `Body` de texte libre doit rester du
    texte libre (interprété par le LLM), seul un vrai payload de clic déclenche
    le bypass. Le log `TWILIO_INBOUND_FORM` ci-dessous permet de confirmer les
    noms EXACTS des champs sur VOTRE compte (à ajuster si besoin).
    """
    for key in ("ButtonPayload", "ListId", "SelectedId", "ListReplyId"):
        val = form.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


@router.post("/webhook/twilio")
async def twilio_webhook(
    request: Request,
    background_tasks: BackgroundTasks, # ◄ FastAPI gère ça automatiquement
    From: str = Form(...),
    Body: str = Form(""),
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

    # --- DÉTECTION DU CLIC INTERACTIF (bouton / liste) ---
    # Lit le form complet pour repérer un payload de clic. Loggé UNE fois en
    # entier pour confirmer les noms de champs exacts de votre compte Twilio.
    form = dict(await request.form())
    interactive_id = _extract_interactive_id(form)
    if interactive_id:
        logger.info("TWILIO_INBOUND_INTERACTIVE | phone=%s | id=%s | form_keys=%s",
                    phone, interactive_id, list(form.keys()))

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
        interactive_id=interactive_id,   # ← clic bouton/liste → bypass LLM
    )

    # --- 2. AJOUTER : Message d'attente immédiat via BackgroundTasks ---
    # Cette tâche s'exécutera juste après que FastAPI ait renvoyé le "200 OK" à Twilio.
    # L'agriculteur reçoit le SMS sans que le webhook ne soit ralenti.
    background_tasks.add_task(send_wait_message, phone)

    return {"status": "accepted"}