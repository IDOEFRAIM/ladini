"""Twilio WhatsApp Webhook dispatch brut vers l'Orchestrator unique.

Le webhook ne route plus : il transmet (phone, text) à l'Orchestrator via
Celery. C'est le WorkspaceResolver qui décide l'agent, de façon collante.
"""
import logging
from typing import Optional

import redis

from fastapi import APIRouter, BackgroundTasks, Form, Request, Response

from agriconnect.api.tasks import process_agent_task
from agriconnect.graphs.roles import normalize_role
from agriconnect.workspace.store import WorkspaceStore
from agriconnect.core.settings import settings

router = APIRouter()
logger = logging.getLogger("AgriConnect.TwilioWebhook")


def _empty_twiml() -> Response:
    """Renvoie une réponse TwiML vide valide (text/xml) requise par Twilio."""
    return Response(content="<Response></Response>", media_type="application/xml")


# Statuts de livraison sortants à ignorer (callbacks de statut)
_DELIVERY_STATUSES = frozenset({
    "accepted", "queued", "sending", "sent",
    "delivered", "read", "undelivered", "failed", "canceled",
})

# Initialisation du client Redis
redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)


def _extract_interactive_id(form: dict) -> Optional[str]:
    """Extrait le payload d'un clic WhatsApp (bouton quick-reply / ligne de liste)."""
    for key in ("ButtonPayload", "ListId", "SelectedId", "ListReplyId"):
        val = form.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def _extract_location(form: dict) -> Optional[tuple[float, float, Optional[str]]]:
    """Extrait une position GPS native WhatsApp (message de type "Localisation").

    Twilio expose les coordonnées d'un message de localisation via les champs
    de formulaire `Latitude`/`Longitude` (et `Address`/`Label` si l'utilisateur
    a nommé le lieu). Retourne None si absent ou invalide — ne lève JAMAIS
    d'exception (l'absence de position ne doit jamais interrompre le webhook).
    """
    raw_lat = form.get("Latitude")
    raw_lon = form.get("Longitude")
    if raw_lat in (None, "") or raw_lon in (None, ""):
        return None
    try:
        lat = float(raw_lat)
        lon = float(raw_lon)
    except (TypeError, ValueError):
        logger.warning("TWILIO_WEBHOOK_LOCATION_INVALID | lat=%r | lon=%r", raw_lat, raw_lon)
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        logger.warning("TWILIO_WEBHOOK_LOCATION_OUT_OF_RANGE | lat=%s | lon=%s", lat, lon)
        return None
    address = form.get("Address") or form.get("Label") or None
    return lat, lon, address


async def _persist_location_background(phone: str, lat: float, lon: float) -> None:
    """Enregistre la position GPS en tâche de fond — jamais bloquant pour Twilio.

    Découplé du pipeline agent (Celery/LangGraph) à dessein : la persistance
    doit réussir même si l'agent conversationnel échoue, timeout ou est
    surchargé. Best-effort : toute erreur est loguée, jamais propagée.
    """
    try:
        from agriconnect.services.database.auth import AuthMixin
        from agriconnect.core.database import get_sessionmaker

        class _GeoOnlyService(AuthMixin):
            def __init__(self, session):
                self._session = session

            @property
            def session(self):
                return self._session

        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("TWILIO_WEBHOOK_LOCATION_SKIPPED | sessionmaker indisponible")
            return
        async with sessionmaker() as session:
            service = _GeoOnlyService(session)
            result = await service.update_geo_location(phone=phone, lat=lat, lon=lon)
            if str(result.get("status")) == "success":
                await session.commit()
                logger.info("TWILIO_WEBHOOK_LOCATION_SAVED | phone=***%s", phone[-4:])
            else:
                await session.rollback()
                logger.warning(
                    "TWILIO_WEBHOOK_LOCATION_NOT_SAVED | phone=***%s | reason=%s",
                    phone[-4:], result.get("message"),
                )
    except Exception:
        logger.exception("TWILIO_WEBHOOK_LOCATION_PERSIST_ERROR | phone=***%s", phone[-4:])


@router.post("/webhook/twilio")
async def twilio_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    From: str = Form(...),
    Body: str = Form(""),
    MessageSid: str = Form(...),
):
    try:
        return await _handle_twilio_webhook(request, background_tasks, From, Body, MessageSid)
    except Exception:
        # Filet de sécurité pour éviter l'erreur Twilio 12300 (Invalid Content-Type)
        logger.exception("TWILIO_WEBHOOK_UNHANDLED_ERROR")
        return _empty_twiml()


async def _handle_twilio_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    From: str,
    Body: str,
    MessageSid: str,
):
    # --- 1. ANTI-DOUBLON / IDEMPOTENCE (REDIS) ---
    redis_key = f"msg:{MessageSid}"
    try:
        # Renvoie True uniquement si la clé n'existait pas (nouveau message)
        is_new_message = redis_client.set(redis_key, "processing", ex=3600, nx=True)
        if not is_new_message:
            logger.warning("TWILIO_WEBHOOK_DUPLICATE | MessageSid=%s déjà en cours ou traité", MessageSid)
            return _empty_twiml()
    except Exception as e:
        # Si Redis flanche, on logue l'erreur mais on laisse passer le message (résilience)
        logger.error("TWILIO_WEBHOOK_REDIS_ERROR | Impossible de vérifier l'idempotence: %s", e)

    # --- 2. NETTOYAGE DU NUMÉRO DE TÉLÉPHONE ---
    raw_sender = From
    phone = raw_sender.replace("whatsapp:", "").strip()
    if not phone:
        logger.warning("TWILIO_WEBHOOK_INVALID_SENDER | From=%r", From)
        return _empty_twiml()

    text = Body.strip()
    form = dict(await request.form())

    # --- 3. FILTRAGE DES CALLBACKS DE STATUT ---
    status_value = (form.get("MessageStatus") or form.get("SmsStatus") or "").strip().lower()
    if status_value in _DELIVERY_STATUSES:
        logger.info(
            "TWILIO_STATUS_CALLBACK ignoré | sid=%s | status=%s",
            MessageSid, status_value,
        )
        return _empty_twiml()

    # --- 4. EXTRACTION INTERACTIVE & TÉLÉMÉTRIE ---
    interactive_id = _extract_interactive_id(form)
    if interactive_id:
        logger.info(
            "TWILIO_INBOUND_INTERACTIVE | phone=%s | id=%s | form_keys=%s",
            phone, interactive_id, list(form.keys())
        )

    # --- 4bis. CAPTURE GPS NATIVE (message de localisation WhatsApp) ---
    # Persistance en tâche de fond, DÉCOUPLÉE du pipeline agent : la position
    # doit être enregistrée même si l'agent échoue/timeout. `location_shared`
    # est en plus transmis à l'agent pour qu'il puisse acquitter la réception
    # pendant l'étape finale d'onboarding — voir agents/onboarding.py.
    location = _extract_location(form)
    location_shared = False
    if location:
        lat, lon, address = location
        logger.info(
            "TWILIO_INBOUND_LOCATION | phone=%s | lat=%s | lon=%s | address=%r",
            phone, lat, lon, address,
        )
        background_tasks.add_task(_persist_location_background, phone, lat, lon)
        location_shared = True

    try:
        from agriconnect.core import telemetry
        telemetry.count_webhook("twilio")
        trace_id = getattr(request.state, "trace_id", None) or telemetry.new_trace_id()
    except Exception:
        trace_id = None

    # --- 5. RÉSOLUTION DU WORKSPACE & RÔLE ---
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

    # --- 6. DÉLÉGATION À CELERY ---
    process_agent_task.delay(
        phone_number=phone,
        user_query=text,
        workspace_type=ws_type,
        role=resolved_role,
        force_role=force_role,
        interactive_id=interactive_id,   # Bypass LLM si clic bouton/liste
        trace_id=trace_id,               # Propagation de la trace d'observabilité
        location_shared=location_shared, # Position GPS reçue ce tour (accusé onboarding)
    )

    return _empty_twiml()