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

import asyncio
import logging
from typing import Any, Dict, List, Optional

import redis
from fastapi import APIRouter, BackgroundTasks, Request, Response

from ladini.api.security import (
    is_explicit_dev_mode,
    verify_whatsapp_cloud_signature,
)
from ladini.api.tasks import process_agent_task
from ladini.core.idempotency import get_cached as _get_role_hint
from ladini.core.idempotency import release as _release_role_hint
from ladini.core.location import LocationOutcome, persist_shared_location
from ladini.core.maintenance import MAINTENANCE_MESSAGE, is_celery_producer_paused
from ladini.core.settings import settings
from ladini.graphs.roles import normalize_role
from ladini.workers.media.product_photo_task import (
    pending_photo_key,
    pending_view_key,
    process_product_photo_task,
    resolve_pending_product_photo_task,
    resolve_pending_view_photos_task,
    send_product_photos_task,
    send_search_result_photos_task,
)
from ladini.workspace.store import WorkspaceStore

router = APIRouter()
logger = logging.getLogger("Ladini.WhatsAppWebhook")

# Statuts de livraison sortants — miroir de _DELIVERY_STATUSES dans
# twilio_webhook.py. `sent`/`delivered`/`read` ne déclenchent aucune action
# (juste un accusé de réception attendu du flux normal). `failed` en est
# retiré (voir `_log_failed_status` ci-dessous, 2026-09-11) : un wamid accepté
# par l'API (HTTP 200) ne signifie JAMAIS "livré" — seul ce callback dit si
# l'envoi a réellement échoué, et pourquoi.
_STATUS_EVENTS_TO_IGNORE = frozenset({"sent", "delivered", "read"})

# (2026-09-17, follow-up pre-Hetzner) — borne dure sur `process_agent_task.
# delay()` (voir son site d'appel) : garantit qu'un webhook ne reste jamais
# accroché plus longtemps que ça, quel que soit le comportement interne de
# kombu/Celery pendant une panne broker.
_CELERY_DELAY_TIMEOUT_S = 5.0

redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)


def _plain_ok(body: str = "EVENT_RECEIVED") -> Response:
    return Response(content=body, media_type="text/plain")


# (2026-09-28, hardening P1-B) — miroir volontaire de twilio_webhook.py :
# distingue un échec réel d'enqueue Celery (`.delay()` lève, timeout) d'une
# erreur inattendue quelconque, pour renvoyer un 503 (redélivraison Meta)
# plutôt que le 200 générique du filet de sécurité de `whatsapp_webhook()`.
_ENQUEUE_FAILED_MESSAGE = (
    "Message temporairement indisponible. Réessaie dans un instant."
)


class _EnqueueFailed(Exception):
    """Signale, jusqu'à `whatsapp_webhook()`, qu'un message précis de ce lot
    n'a pas pu être remis à Celery — jamais laissé se confondre avec le
    filet `except Exception` générique (qui renvoie 200, fail-open pour
    tout le reste)."""


async def _release_message_claim(message_id: str) -> None:
    """Miroir de `twilio_webhook.py::_release_message_claim` — voir sa
    docstring pour le mécanisme complet. Même rationale : sans ce
    relâchement, la réclamation `msg:{id}` (TTL 3600s) posée en §1 bloque
    silencieusement la redélivraison Meta déclenchée par le 503 renvoyé
    juste après (§6 ci-dessous, ou la pause maintenance)."""
    try:
        await asyncio.to_thread(redis_client.delete, f"msg:{message_id}")
    except Exception as e:  # pragma: no cover - défensif
        logger.error(
            "WHATSAPP_WEBHOOK_CLAIM_RELEASE_FAILED | id=%s | error=%s",
            message_id,
            e,
        )


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


def _log_failed_status(st: Dict[str, Any]) -> None:
    """Journalise un échec de LIVRAISON réel (2026-09-11 — étape 1 du
    diagnostic « message marqué SENT mais jamais reçu »).

    Un `POST /messages` qui répond 200 + un `wamid` (voir
    `services/whatsapp/cloud_api_client.py::_post`) veut seulement dire
    « Meta a accepté d'essayer » — jamais « livré ». La livraison réelle
    (ou son échec) arrive ICI, en asynchrone, via ce callback `statuses`.
    Avant ce correctif, `failed` était traité exactement comme
    `delivered`/`read` (même branche `_STATUS_EVENTS_TO_IGNORE`, un simple
    `logger.info` du mot "failed") — le tableau `errors` de Meta, qui porte
    le VRAI motif (ex: code 131047 "Re-engagement message" = la fenêtre de
    24h de conversation est dépassée — cas typique des notifications
    proactives de l'Outbox : confirmation de commande, résultat d'enchère,
    envoyées par un cron, pas en réponse immédiate à un message entrant),
    n'était jamais lu. Ce webhook ne fait encore QUE journaliser : aucune
    action corrective automatique (retry, template de secours...) — voir le
    diagnostic complet avant de décider de l'étape 2.
    """
    wamid = str(st.get("id") or "").strip()
    recipient = str(st.get("recipient_id") or "").strip()
    errors = st.get("errors") or []
    if not errors:
        # Statut "failed" sans détail — arrive rarement, mais ne doit jamais
        # passer inaperçu (c'est justement le trou qu'on comble ici).
        logger.warning(
            "WHATSAPP_DELIVERY_FAILED | wamid=%s | recipient=%s | "
            "(Meta n'a fourni aucun détail d'erreur)",
            wamid,
            recipient,
        )
        return
    for err in errors:
        if not isinstance(err, dict):
            continue
        details = (err.get("error_data") or {}).get("details") or ""
        logger.warning(
            "WHATSAPP_DELIVERY_FAILED | wamid=%s | recipient=%s | "
            "code=%s | title=%s | message=%s | details=%s",
            wamid,
            recipient,
            err.get("code"),
            err.get("title"),
            err.get("message"),
            details,
        )


def _extract_media_id(message: Dict[str, Any]) -> Optional[tuple[str, str]]:
    """Extrait une image entrante Cloud API (``message["image"]``).

    Miroir de ``twilio_webhook.py::_extract_media`` pour le format Cloud
    API — mais contrairement à Twilio (URL directe fetchable), Meta ne
    donne ici qu'un ``id`` OPAQUE (``message["image"]["id"]``), résolu en
    URL téléchargeable UNIQUEMENT au moment du téléchargement (voir
    ``services/whatsapp/cloud_api_media.py``). Renvoie ``(media_id,
    mime_type)`` pour un message de type ``image`` uniquement — audio/vidéo/
    document/document ou texte classique renvoient ``None``.
    """
    if str(message.get("type") or "").strip() != "image":
        return None
    image = message.get("image")
    if not isinstance(image, dict):
        return None
    media_id = str(image.get("id") or "").strip()
    if not media_id:
        return None
    mime_type = str(image.get("mime_type") or "").strip().lower()
    return media_id, mime_type


_VIEW_PHOTOS_PREFIXES = ("photos ", "photo ")


def _extract_view_photos_query(text: str) -> Optional[str]:
    """Commande déterministe « photos <nom> » / « photo <nom> » — MIROIR
    VOLONTAIRE de ``twilio_webhook.py::_extract_view_photos_query`` (même
    sous-ensemble de commande, même raisonnement : mot-clé simple, pas une
    intention LLM — voir l'original pour le détail)."""
    stripped = text.strip()
    lowered = stripped.lower()
    for prefix in _VIEW_PHOTOS_PREFIXES:
        if lowered.startswith(prefix):
            query = stripped[len(prefix) :].strip()
            return query or None
    return None


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
    except _EnqueueFailed:
        # (2026-09-28, hardening P1-B) : ce cas précis (échec d'enqueue Celery
        # pour un message de ce lot) doit provoquer une redélivraison Meta,
        # PAS le 200 fail-open du filet générique ci-dessous — voir
        # `_process_single_message` pour l'endroit où la réclamation Redis
        # de CE message a déjà été relâchée avant que cette exception ne
        # remonte jusqu'ici.
        logger.error("WHATSAPP_WEBHOOK_ENQUEUE_FAILED")
        return Response(
            content=_ENQUEUE_FAILED_MESSAGE, media_type="text/plain", status_code=503
        )
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

    # (2026-09-20, bascule Upstash → Valkey) — voir core/maintenance.py.
    # Vérifié ICI (après la signature, avant tout traitement de message) :
    # un webhook peut porter PLUSIEURS messages (boucle plus bas), un seul
    # check groupé évite de produire une tâche pour certains messages du
    # lot et pas d'autres. 503 plutôt que `_plain_ok()` : WhatsApp Cloud API
    # redélivre ce webhook après un échec — acceptable UNIQUEMENT parce que
    # la fenêtre de maintenance visée est courte (1-3 minutes) et ponctuelle
    # (voir docstring de core/maintenance.py pour le tradeoff détaillé).
    if is_celery_producer_paused():
        # (2026-09-28, hardening P1-B) : contrairement à twilio_webhook.py,
        # ce check a lieu AVANT tout parsing/réclamation de message (aucune
        # clé `msg:{id}` n'est encore posée à ce stade) — rien à relâcher ici.
        logger.info("WHATSAPP_WEBHOOK_PAUSED — maintenance broker en cours")
        return Response(
            content=MAINTENANCE_MESSAGE, media_type="text/plain", status_code=503
        )

    payload = await request.json()

    entries: List[Dict[str, Any]] = payload.get("entry") or []
    for entry in entries:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}

            # --- Callbacks de statut sortant ---
            statuses = value.get("statuses") or []
            for st in statuses:
                st_type = str(st.get("status") or "").lower()
                if st_type == "failed":
                    _log_failed_status(st)
                elif st_type in _STATUS_EVENTS_TO_IGNORE:
                    logger.info("WHATSAPP_STATUS_CALLBACK ignoré | status=%s", st_type)

            messages = value.get("messages") or []
            for message in messages:
                await _process_single_message(message, background_tasks)

    return _plain_ok()


async def _process_single_message(
    message: Dict[str, Any], background_tasks: BackgroundTasks
) -> None:
    # --- 1. ANTI-DOUBLON / IDEMPOTENCE (REDIS) ---
    # `redis_client` (module-level) est le client SYNCHRONE `redis` — pas
    # `redis.asyncio`. L'appeler directement ici bloquerait la boucle
    # asyncio du process gunicorn/uvicorn pendant tout le round-trip réseau
    # (confirmé par charge k6 : throughput plafonné ~42 req/s quel que soit
    # le nombre de VUs, latence webhook croissant linéairement avec la
    # concurrence — signature classique d'un appel bloquant synchrone dans
    # un handler async). `asyncio.to_thread` délègue l'appel bloquant à un
    # thread du pool par défaut au lieu de geler la boucle événementielle —
    # coût minimal (le pool grossit dynamiquement, et ces appels Redis sont
    # courts, contrairement à l'incident historique documenté dans
    # `core/get_llm.py` sur des appels HTTP longs sans timeout serré).
    message_id = str(message.get("id") or "").strip()
    if message_id:
        redis_key = f"msg:{message_id}"
        try:
            is_new_message = await asyncio.to_thread(
                redis_client.set, redis_key, "processing", ex=3600, nx=True
            )
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

    # --- 4bis. PHOTO PRODUIT (MEDIA) ---
    # ⚠️ CORRECTIF (2026-09-19, incident réel prod) — ce bloc, présent côté
    # `twilio_webhook.py` depuis la feature "photo produit par WhatsApp",
    # n'avait JAMAIS été porté ici. Comme `whatsapp_cloud` est le
    # `MESSAGING_PROVIDER` par défaut (voir en-tête de ce fichier), TOUTE
    # photo envoyée par un producteur/acheteur en production tombait
    # silencieusement dans le pipeline texte (`text=""`, `msg_type="image"`
    # jamais lu) — l'intention finissait en `UNKNOWN` côté agent, qui
    # répondait par le message générique de clarification ("Je n'ai pas
    # compris ton message avec la photo..."). La fonctionnalité de photo
    # produit était donc entièrement morte en production tant que
    # `MESSAGING_PROVIDER` n'était pas manuellement basculé sur `twilio`.
    #
    # Miroir volontaire de `twilio_webhook.py` (§4ter/4quater/4quinquies) —
    # même découplage du pipeline agent, seule la source du média change
    # (`media_id` Cloud API vs `media_url` Twilio, voir
    # `services/whatsapp/cloud_api_media.py` et `workers/media/
    # product_photo_task.py::_process`).
    media = _extract_media_id(message)
    if media:
        media_id, media_content_type = media
        logger.info(
            "WHATSAPP_INBOUND_MEDIA | phone=%s | content_type=%s", phone, media_content_type
        )
        await asyncio.to_thread(
            process_product_photo_task.delay,
            phone_number=phone,
            media_id=media_id,
            media_content_type=media_content_type,
            trace_id=trace_id,
            message_sid=message_id,
        )
        return

    # --- 4ter. RÉPONSE À UN MENU "QUEL PRODUIT ?" EN ATTENTE ---
    # Même garde qu'en Twilio : ne court-circuite le pipeline agent QUE si
    # (a) une photo est en attente de sélection pour ce numéro ET (b) le
    # texte est un simple chiffre.
    if text.isdigit():
        try:
            has_pending_photo = bool(
                await asyncio.to_thread(redis_client.exists, pending_photo_key(phone))
            )
        except Exception:
            has_pending_photo = False
        if has_pending_photo:
            await asyncio.to_thread(
                resolve_pending_product_photo_task.delay,
                phone_number=phone, selection_text=text, message_sid=message_id,
            )
            return

        try:
            has_pending_view = bool(
                await asyncio.to_thread(redis_client.exists, pending_view_key(phone))
            )
        except Exception:
            has_pending_view = False
        if has_pending_view:
            await asyncio.to_thread(
                resolve_pending_view_photos_task.delay,
                phone_number=phone, selection_text=text, message_sid=message_id,
            )
            return

    # --- 4quater. CONSULTATION "PHOTOS <NOM>" / "PHOTOS <NUMÉRO>" ---
    view_query = _extract_view_photos_query(text)
    if view_query:
        logger.info("WHATSAPP_INBOUND_VIEW_PHOTOS | phone=%s | query=%r", phone, view_query)
        if view_query.isdigit():
            await asyncio.to_thread(
                send_search_result_photos_task.delay,
                phone_number=phone, index_text=view_query, message_sid=message_id,
            )
        else:
            await asyncio.to_thread(
                send_product_photos_task.delay,
                phone_number=phone, product_query=view_query, message_sid=message_id,
            )
        return

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

    # (2026-09-13, incident WhatsApp #3 — utilisateur double-rôle) : même
    # correctif que `twilio_webhook.py` — ce webhook fige déjà `force_role=True`
    # dès qu'un workspace existe, ce qui court-circuite le correctif posé dans
    # `orchestrator.py::_run_market` (il ne s'exécute jamais quand `force_role`
    # est déjà True). `ws_type` ci-dessus est le dernier rôle utilisé dans CE
    # workspace (sticky, même numéro pour buyer/producteur) — potentiellement
    # périmé pour un producteur qui répond "confirmer"/"annuler" à une
    # notification de commande reçue alors que son workspace est resté en mode
    # BUYER. `flows/buyer/preorder_confirmation.py` pose un indice d'ÉTAT plus
    # récent (`pending_role_hint:{phone}`) juste après avoir mis CETTE
    # notification producteur en file — on le lit et le consomme ICI, AVANT de
    # figer le rôle. Fail-open comme tout `core/idempotency.py`.
    # `core/idempotency.py` expose un client Redis SYNCHRONE (partagé avec
    # des appelants Celery sync) — `asyncio.to_thread` ici pour la même
    # raison que le garde anti-doublon ci-dessus.
    role_hint = await asyncio.to_thread(_get_role_hint, f"pending_role_hint:{phone}")
    if role_hint:
        await asyncio.to_thread(_release_role_hint, f"pending_role_hint:{phone}")
        hint_up = str(role_hint).strip().upper()
        if hint_up in {"PRODUCER", "PRODUCTEUR", "PRODUCTRICE"}:
            ws_type = "producer"
            resolved_role = normalize_role("PRODUCER")
            force_role = True
        elif hint_up in {"BUYER", "ACHETEUR", "ACHETEUSE"}:
            ws_type = "buyer"
            resolved_role = normalize_role("BUYER")
            force_role = True

    logger.info(
        "WhatsApp Cloud webhook | phone=%s | type=%s | text=%r | existing_workspace=%s",
        phone,
        msg_type,
        text[:80],
        bool(workspace),
    )

    # --- 6. DÉLÉGATION À CELERY ---
    # `.delay()` (kombu, transport Redis) est SYNCHRONE et bloquante — même
    # défaut que les appels Redis directs déjà corrigés plus haut dans ce
    # fichier (asyncio.to_thread), mais PAS entièrement couvert par ce seul
    # correctif : confirmé en E2E local (2026-09-17) qu'un webhook pouvait
    # rester bloqué PLUSIEURS DIZAINES DE SECONDES pendant une panne Redis
    # complète, MALGRÉ `asyncio.to_thread` + le tuning `socket_connect_
    # timeout`/`broker_connection_max_retries` (celery_app.py) — le thread
    # sous-jacent restait lui-même accroché plus longtemps que ces réglages
    # ne le laissaient supposer (kombu/celery, chemin PRODUCTEUR). Plutôt
    # que de continuer à chasser le réglage kombu exact, `asyncio.wait_for`
    # BORNE la coroutine elle-même — garantie dure, indépendante de ce que
    # fait le thread en interne (qui peut continuer de tourner en
    # arrière-plan jusqu'à sa propre résolution, sans bloquer CETTE requête
    # plus de `_CELERY_DELAY_TIMEOUT_S`). `TimeoutError` remonte à
    # l'appelant (`whatsapp_webhook()`), déjà `except Exception` large —
    # renvoie 200 quand même (fail-open, cohérent avec le reste de ce
    # fichier), le message est simplement perdu pour cette tentative plutôt
    # que de geler la requête.
    # (2026-09-28, hardening P1-B) : AVANT ce correctif, une exception ICI
    # (broker injoignable, `.delay()` qui lève, ou le timeout) remontait
    # jusqu'au filet générique de `whatsapp_webhook()` (`except Exception:
    # return _plain_ok()`) — un 200. Meta considère alors CE MESSAGE livré
    # (aucune redélivraison), alors que la réclamation `msg:{id}` posée en
    # §1 (TTL 1h) bloquait silencieusement toute reprise : le message
    # disparaissait sans réponse, sans trace exploitable. `_EnqueueFailed`
    # distingue maintenant ce cas précis — voir son site de capture dans
    # `whatsapp_webhook()` (503, donc redélivraison Meta du lot).
    try:
        await asyncio.wait_for(
            asyncio.to_thread(
                process_agent_task.delay,
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
            ),
            timeout=_CELERY_DELAY_TIMEOUT_S,
        )
    except Exception as e:
        logger.error(
            "WHATSAPP_WEBHOOK_ENQUEUE_FAILED | phone=%s | id=%s | error=%s",
            phone,
            message_id,
            e,
        )
        if message_id:
            await _release_message_claim(message_id)
        raise _EnqueueFailed(message_id) from e


__all__ = ["router"]
