"""Twilio WhatsApp Webhook dispatch brut vers l'Orchestrator unique.

Le webhook ne route plus : il transmet (phone, text) à l'Orchestrator via
Celery. C'est le WorkspaceResolver qui décide l'agent, de façon collante.
"""
import logging
from typing import Optional

import redis # ◄ N'oublie pas : pip install redis

from fastapi import (
  APIRouter, Form, BackgroundTasks, Request, Response
)
from twilio.rest import Client

from agriconnect.api.tasks import process_agent_task
from agriconnect.graphs.roles import normalize_role
from agriconnect.workspace.store import WorkspaceStore
from agriconnect.api.security import verify_twilio_signature
# Assure-toi d'importer tes settings (à adapter selon ton projet)
from agriconnect.core.settings import settings 

router = APIRouter()
logger = logging.getLogger("AgriConnect.TwilioWebhook")

# Twilio attend soit un corps vide, soit du TwiML valide (Content-Type text/xml ou
# application/xml) en réponse à un webhook de message entrant. Renvoyer un dict
# JSON (Content-Type: application/json, comportement par défaut de FastAPI) fait
# échouer Twilio avec l'erreur 12300 "Invalid Content-Type" — le message est bien
# reçu par notre webhook (200 OK dans les logs) mais Twilio considère l'échange
# en erreur et n'achemine pas la suite normalement.
def _empty_twiml() -> Response:
    return Response(content="<Response></Response>", media_type="application/xml")


# Valeurs de `MessageStatus`/`SmsStatus` correspondant à des ACCUSÉS DE LIVRAISON
# (à ignorer). On y met SEULEMENT les statuts sortants — surtout PAS `received`,
# qui est la valeur portée par un vrai message ENTRANT (à traiter normalement).
_DELIVERY_STATUSES = frozenset({
    "accepted", "queued", "sending", "sent",
    "delivered", "read", "undelivered", "failed", "canceled",
})

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
        if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN or not settings.TWILIO_WHATSAPP_NUMBER:
            return
            
        # Nettoyage des numéros pour comparer proprement (retrait de 'whatsapp:', espaces, etc.)
        from_cleaned = settings.TWILIO_WHATSAPP_NUMBER.replace("whatsapp:", "").strip()
        to_cleaned = phone_number.replace("whatsapp:", "").strip()

        # Évite l'erreur 400 si l'expéditeur et le destinataire sont identiques (test local)
        if from_cleaned == to_cleaned:
            logger.warning("Impossible d'envoyer le message d'attente : Le numéro expéditeur et destinataire sont identiques (%s)", to_cleaned)
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
    try:
        return await _handle_twilio_webhook(request, background_tasks, From, Body, MessageSid)
    except Exception:
        # Filet de sécurité : toute exception non prévue ne doit JAMAIS s'échapper
        # sous forme de réponse JSON (Content-Type par défaut de FastAPI) — Twilio
        # rejette ça avec l'erreur 12300 "Invalid Content-Type" et n'achemine rien.
        logger.exception("TWILIO_WEBHOOK_UNHANDLED_ERROR")
        return _empty_twiml()


async def _handle_twilio_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    From: str,
    Body: str,
    MessageSid: str,
):
    # --- 1. MODIFIER : ANTI-DOUBLON (Idempotence avec Redis) ---
    redis_key = f"msg:{MessageSid}"
    is_new_message = redis_client.set(redis_key, "processing", ex=600, nx=True)

    

    # On marque le message comme "en cours" pendant 1 heure (3600 secondes)
    redis_client.setex(redis_key, 3600, "processing")
    # -----------------------------------------------------------

    raw_sender = From
    phone = raw_sender.replace("whatsapp:", "").strip()
    if not phone:
        logger.warning("TWILIO_WEBHOOK_INVALID_SENDER | From=%r", From)
        return _empty_twiml()

    text = Body.strip()

    # --- DÉTECTION DU CLIC INTERACTIF (bouton / liste) ---
    # Lit le form complet pour repérer un payload de clic. Loggé UNE fois en
    # entier pour confirmer les noms de champs exacts de votre compte Twilio.
    form = dict(await request.form())

    # --- CALLBACK DE STATUT (delivered/read/failed), PAS UN MESSAGE ---
    # Si l'URL de status callback Twilio est configurée sur cette même route
    # (webhook de messagerie et de statut confondus — cas fréquent en sandbox),
    # chaque message ENVOYÉ par le bot déclenche ICI un accusé de livraison :
    # `From` porte alors le numéro DU BOT (ex : +14155238886, sandbox) et
    # `MessageStatus`/`SmsStatus` porte une valeur de LIVRAISON.
    #
    # PIÈGE CRITIQUE (corrigé) : un VRAI message entrant Twilio WhatsApp porte
    # LUI AUSSI le champ `SmsStatus`, avec la valeur `received`. L'ancien garde
    # `if form.get("SmsStatus")` attrapait donc TOUS les vrais messages entrants
    # et les jetait comme de faux accusés de statut → webhook 200 OK mais tâche
    # Celery jamais déclenchée (bug : "ça marche avec curl mais pas via WhatsApp",
    # car curl n'envoyait pas de SmsStatus). On ne saute QUE sur les vraies
    # valeurs de livraison, jamais sur `received` (= message entrant à traiter).
    status_value = (form.get("MessageStatus") or form.get("SmsStatus") or "").strip().lower()
    if status_value in _DELIVERY_STATUSES:
        logger.info(
            "TWILIO_STATUS_CALLBACK ignoré | sid=%s | status=%s",
            MessageSid, status_value,
        )
        return _empty_twiml()

    interactive_id = _extract_interactive_id(form)
    if interactive_id:
        logger.info("TWILIO_INBOUND_INTERACTIVE | phone=%s | id=%s | form_keys=%s",
                    phone, interactive_id, list(form.keys()))

    # --- OBSERVABILITÉ : compteur + trace_id de bout en bout ---
    # Le middleware a posé request.state.trace_id (format OTel, réutilisé comme
    # id de Trace Langfuse). On le propage au worker via la tâche Celery pour
    # relier webhook → worker → graphe → appel LLM sous UNE seule Trace.
    try:
        from agriconnect.core import telemetry
        telemetry.count_webhook("twilio")
        trace_id = getattr(request.state, "trace_id", None) or telemetry.new_trace_id()
    except Exception:
        trace_id = None

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
        trace_id=trace_id,               # ← propagation trace infra → LLM
    )

    # --- 2. AJOUTER : Message d'attente immédiat via BackgroundTasks ---
    # Cette tâche s'exécutera juste après que FastAPI ait renvoyé le "200 OK" à Twilio.
    # L'agriculteur reçoit le SMS sans que le webhook ne soit ralenti.
    background_tasks.add_task(send_wait_message, phone)

    return _empty_twiml()