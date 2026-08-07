"""WhatsApp Cloud API (Meta) Webhook — remplace ``twilio_webhook.py``.

Même contrat métier que le webhook Twilio (idempotence Redis, capture GPS en
tâche de fond, bypass interactif, délégation Celery) — seule la couche de
parsing change : payload JSON Graph API au lieu de form-data Twilio, ID de
message ``wamid.*`` au lieu de ``MessageSid``, signature HMAC-SHA256 sur le
corps brut au lieu d'une signature Twilio sur l'URL+paramètres.

Actif uniquement quand ``settings.MESSAGING_PROVIDER == "whatsapp_cloud"``
(voir ``api/main.py``) — le webhook Twilio reste disponible en parallèle
pour un rollback instantané.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import redis
from fastapi import APIRouter, BackgroundTasks, Request, Response

from agriconnect.api.security import verify_whatsapp_cloud_signature
from agriconnect.api.tasks import process_agent_task
from agriconnect.graphs.roles import normalize_role
from agriconnect.workspace.store import WorkspaceStore
from agriconnect.core.settings import settings

router = APIRouter()
logger = logging.getLogger("AgriConnect.WhatsAppWebhook")

# Statuts de livraison sortants à ignorer (accusés delivered/read/sent/failed) —
# miroir de _DELIVERY_STATUSES dans twilio_webhook.py.
_STATUS_EVENTS_TO_IGNORE = frozenset({"sent", "delivered", "read", "failed"})

redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)


def _plain_ok(body: str = "EVENT_RECEIVED") -> Response:
    return Response(content=body, media_type="text/plain")


# =====================================================================
# GET — Vérification du webhook (configuration initiale dans Meta for Developers)
# =====================================================================

@router.get("/webhook/whatsapp")
async def verify_whatsapp_webhook(request: Request) -> Response:
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    expected_token = str(settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN or "").strip()
    if mode == "subscribe" and expected_token and token == expected_token:
        logger.info("WHATSAPP_WEBHOOK_VERIFIED")
        return Response(content=challenge or "", media_type="text/plain")

    logger.warning("WHATSAPP_WEBHOOK_VERIFICATION_FAILED | mode=%s", mode)
    return Response(content="Forbidden", media_type="text/plain", status_code=403)


# =====================================================================
# POST — Réception des messages
# =====================================================================

def _extract_location(message: Dict[str, Any]) -> Optional[tuple[float, float, Optional[str]]]:
    """Miroir de ``twilio_webhook.py::_extract_location`` pour le format Cloud API."""
    loc = message.get("location")
    if not isinstance(loc, dict):
        return None
    raw_lat, raw_lon = loc.get("latitude"), loc.get("longitude")
    if raw_lat is None or raw_lon is None:
        return None
    try:
        lat, lon = float(raw_lat), float(raw_lon)
    except (TypeError, ValueError):
        logger.warning("WHATSAPP_WEBHOOK_LOCATION_INVALID | lat=%r | lon=%r", raw_lat, raw_lon)
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        logger.warning("WHATSAPP_WEBHOOK_LOCATION_OUT_OF_RANGE | lat=%s | lon=%s", lat, lon)
        return None
    address = loc.get("address") or loc.get("name") or None
    return lat, lon, address


def _extract_interactive_id(message: Dict[str, Any]) -> Optional[str]:
    """Bouton/liste tapé par l'utilisateur — équivalent de ``ButtonPayload``/
    ``ListId`` côté Twilio. Le payload Cloud API est structurellement différent
    (JSON imbriqué au lieu de champs de formulaire à plat)."""
    interactive = message.get("interactive")
    if not isinstance(interactive, dict):
        return None
    kind = interactive.get("type")
    if kind == "button_reply":
        val = (interactive.get("button_reply") or {}).get("id")
    elif kind == "list_reply":
        val = (interactive.get("list_reply") or {}).get("id")
    else:
        val = None
    return str(val).strip() if val else None


async def _persist_location_background(phone: str, lat: float, lon: float) -> None:
    """Identique à la version Twilio — persistance découplée, best-effort."""
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
            logger.warning("WHATSAPP_WEBHOOK_LOCATION_SKIPPED | sessionmaker indisponible")
            return
        async with sessionmaker() as session:
            service = _GeoOnlyService(session)
            result = await service.update_geo_location(phone=phone, lat=lat, lon=lon)
            if str(result.get("status")) == "success":
                await session.commit()
                logger.info("WHATSAPP_WEBHOOK_LOCATION_SAVED | phone=***%s", phone[-4:])
            else:
                await session.rollback()
                logger.warning(
                    "WHATSAPP_WEBHOOK_LOCATION_NOT_SAVED | phone=***%s | reason=%s",
                    phone[-4:], result.get("message"),
                )
    except Exception:
        logger.exception("WHATSAPP_WEBHOOK_LOCATION_PERSIST_ERROR | phone=***%s", phone[-4:])


@router.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
    try:
        return await _handle_whatsapp_webhook(request, background_tasks)
    except Exception:
        logger.exception("WHATSAPP_WEBHOOK_UNHANDLED_ERROR")
        return _plain_ok()


async def _handle_whatsapp_webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
    raw_body = await request.body()

    # --- 0. VÉRIFICATION DE SIGNATURE (anti-usurpation) ---
    app_secret = str(settings.WHATSAPP_APP_SECRET or "").strip()
    if app_secret:
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not verify_whatsapp_cloud_signature(app_secret, raw_body, signature):
            logger.warning("WHATSAPP_WEBHOOK_BAD_SIGNATURE")
            return Response(content="Forbidden", media_type="text/plain", status_code=403)
    else:
        logger.warning("WHATSAPP_WEBHOOK_SIGNATURE_CHECK_SKIPPED | WHATSAPP_APP_SECRET non configuré")

    payload = await request.json()

    entries: List[Dict[str, Any]] = payload.get("entry") or []
    for entry in entries:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}

            # --- Callbacks de statut (sent/delivered/read) : ignorés ---
            statuses = value.get("statuses") or []
            for st in statuses:
                st_type = str(st.get("status") or "").lower()
                if st_type in _STATUS_EVENTS_TO_IGNORE:
                    logger.info("WHATSAPP_STATUS_CALLBACK ignoré | status=%s", st_type)

            messages = value.get("messages") or []
            for message in messages:
                await _process_single_message(message, background_tasks)

    return _plain_ok()


async def _process_single_message(message: Dict[str, Any], background_tasks: BackgroundTasks) -> None:
    # --- 1. ANTI-DOUBLON / IDEMPOTENCE (REDIS) ---
    message_id = str(message.get("id") or "").strip()
    if message_id:
        redis_key = f"msg:{message_id}"
        try:
            is_new_message = redis_client.set(redis_key, "processing", ex=3600, nx=True)
            if not is_new_message:
                logger.warning("WHATSAPP_WEBHOOK_DUPLICATE | id=%s déjà en cours ou traité", message_id)
                return
        except Exception as e:
            logger.error("WHATSAPP_WEBHOOK_REDIS_ERROR | Impossible de vérifier l'idempotence: %s", e)

    # --- 2. NUMÉRO DE TÉLÉPHONE ---
    phone = str(message.get("from") or "").strip()
    if not phone:
        logger.warning("WHATSAPP_WEBHOOK_INVALID_SENDER | message=%r", message)
        return

    msg_type = str(message.get("type") or "").strip()
    text = ""
    if msg_type == "text":
        text = str((message.get("text") or {}).get("body") or "").strip()

    # --- 3. INTERACTIF (bouton/liste) ---
    interactive_id = _extract_interactive_id(message)
    if interactive_id:
        logger.info("WHATSAPP_INBOUND_INTERACTIVE | phone=%s | id=%s", phone, interactive_id)

    # --- 4. GPS NATIF ---
    location = _extract_location(message)
    location_shared = False
    if location:
        lat, lon, address = location
        logger.info(
            "WHATSAPP_INBOUND_LOCATION | phone=%s | lat=%s | lon=%s | address=%r",
            phone, lat, lon, address,
        )
        background_tasks.add_task(_persist_location_background, phone, lat, lon)
        location_shared = True

    try:
        from agriconnect.core import telemetry
        telemetry.count_webhook("whatsapp_cloud")
        trace_id = telemetry.new_trace_id()
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

    logger.info(
        "WhatsApp Cloud webhook | phone=%s | type=%s | text=%r | existing_workspace=%s",
        phone, msg_type, text[:80], bool(workspace),
    )

    # --- 6. DÉLÉGATION À CELERY ---
    process_agent_task.delay(
        phone_number=phone,
        user_query=text,
        workspace_type=ws_type,
        role=resolved_role,
        force_role=force_role,
        interactive_id=interactive_id,
        trace_id=trace_id,
        location_shared=location_shared,
    )


__all__ = ["router"]
