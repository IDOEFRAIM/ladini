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

from ladini.api.security import (
    is_explicit_dev_mode,
    verify_whatsapp_cloud_signature,
)
from ladini.api.tasks import process_agent_task
from ladini.core.location import LocationOutcome, persist_shared_location
from ladini.core.settings import settings
from ladini.graphs.roles import normalize_role
from ladini.workspace.store import WorkspaceStore

router = APIRouter()
logger = logging.getLogger("Ladini.WhatsAppWebhook")

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


def _extract_location(
    message: Dict[str, Any],
) -> Optional[tuple[float, float, Optional[str]]]:
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
        logger.warning(
            "WHATSAPP_WEBHOOK_LOCATION_INVALID | lat=%r | lon=%r", raw_lat, raw_lon
        )
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        logger.warning(
            "WHATSAPP_WEBHOOK_LOCATION_OUT_OF_RANGE | lat=%s | lon=%s", lat, lon
        )
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


# (2026-09-02) Ancienne persistance `BackgroundTasks` retirée — voir
# `core/location.py::persist_shared_location`, désormais appelée SYNCHRONE
# (`await`) dans `_process_single_message` ci-dessous, AVANT l'enqueue
# Celery. Même correction que `twilio_webhook.py` (miroir intentionnel,
# voir docstring de module) — élimine la course webhook/tâche ET le repli
# silencieux sur une position périmée.


@router.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> Response:
    try:
        return await _handle_whatsapp_webhook(request, background_tasks)
    except Exception:
        logger.exception("WHATSAPP_WEBHOOK_UNHANDLED_ERROR")
        return _plain_ok()


async def _handle_whatsapp_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> Response:
    raw_body = await request.body()

    # --- 0. VÉRIFICATION DE SIGNATURE (anti-usurpation) ---
    #
    # ⚠️ AUDIT SÉCURITÉ 2026-09-10 — ce bloc était FAIL-OPEN : `WHATSAPP_APP_SECRET`
    # absent journalisait un simple warning puis traitait le message quand même.
    # Comme `whatsapp_cloud` est le MESSAGING_PROVIDER par défaut, un secret
    # oublié en production laissait le webhook principal grand ouvert : tout
    # ce qui suit dérive l'identité de l'utilisateur du champ `from` du payload,
    # donc n'importe qui pouvait poster un faux message et faire agir l'agent
    # au nom de n'importe quel numéro. Désormais fail-closed, sauf en
    # développement DÉCLARÉ (`ENV=development`), qui journalise bruyamment.
    app_secret = str(settings.WHATSAPP_APP_SECRET or "").strip()
    if app_secret:
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not verify_whatsapp_cloud_signature(app_secret, raw_body, signature):
            logger.warning("WHATSAPP_WEBHOOK_BAD_SIGNATURE")
            return Response(
                content="Forbidden", media_type="text/plain", status_code=403
            )
    elif is_explicit_dev_mode():
        logger.warning(
            "WHATSAPP_WEBHOOK_SIGNATURE_CHECK_SKIPPED | ENV=development et "
            "WHATSAPP_APP_SECRET non configuré — endpoint NON authentifié."
        )
    else:
        logger.error(
            "WHATSAPP_WEBHOOK_UNVERIFIABLE | WHATSAPP_APP_SECRET non configuré — "
            "webhook refusé (fail-closed)."
        )
        return Response(
            content="Service Unavailable", media_type="text/plain", status_code=503
        )

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


async def _process_single_message(
    message: Dict[str, Any], background_tasks: BackgroundTasks
) -> None:
    # --- 1. ANTI-DOUBLON / IDEMPOTENCE (REDIS) ---
    message_id = str(message.get("id") or "").strip()
    if message_id:
        redis_key = f"msg:{message_id}"
        try:
            is_new_message = redis_client.set(redis_key, "processing", ex=3600, nx=True)
            if not is_new_message:
                logger.warning(
                    "WHATSAPP_WEBHOOK_DUPLICATE | id=%s déjà en cours ou traité",
                    message_id,
                )
                return
        except Exception as e:
            logger.error(
                "WHATSAPP_WEBHOOK_REDIS_ERROR | Impossible de vérifier l'idempotence: %s",
                e,
            )

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
        logger.info(
            "WHATSAPP_INBOUND_INTERACTIVE | phone=%s | id=%s", phone, interactive_id
        )

    # --- 4. GPS NATIF ---
    # (2026-09-02, refonte GPS ; nettoyage architectural final 2026-09-02 —
    # "un seul propriétaire de la réponse") : le webhook ne fait plus QUE
    # normaliser l'événement entrant et persister le fait brut (ÉCRITURE,
    # pas une décision de réponse). Voir le commentaire jumeau, plus complet,
    # dans twilio_webhook.py — l'ancienne notification immédiate faisait du
    # webhook un 2e propriétaire de la réponse, concurrent du tour de graphe
    # normal (`nodes/clarification.py` couvre désormais, SEUL, le cas
    # "aucune étape GPS active").
    location = _extract_location(message)
    location_shared = False
    location_outcome: Optional[str] = None
    location_lat: Optional[float] = None
    location_lon: Optional[float] = None
    if location:
        lat, lon, address = location
        logger.info(
            "WHATSAPP_INBOUND_LOCATION | phone=%s | lat=%s | lon=%s | address=%r",
            phone,
            lat,
            lon,
            address,
        )
        outcome, _user_message = await persist_shared_location(phone, lat, lon)
        location_shared = True
        location_outcome = outcome.value
        if outcome == LocationOutcome.NEW_LOCATION_ACCEPTED:
            location_lat, location_lon = lat, lon

    try:
        from ladini.core import telemetry

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
        phone,
        msg_type,
        text[:80],
        bool(workspace),
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
        location_outcome=location_outcome,
        location_lat=location_lat,
        location_lon=location_lon,
        # (2026-09-02) Clé d'idempotence de l'ENVOI — voir api/tasks.py::
        # _claim_single_response. Même identifiant que la clé de
        # dédoublonnage webhook ci-dessus, réutilisé pour protéger aussi
        # contre un retry Celery de la tâche elle-même.
        message_sid=message_id,
    )


__all__ = ["router"]
