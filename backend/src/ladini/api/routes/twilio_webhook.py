"""Twilio WhatsApp Webhook dispatch brut vers l'Orchestrator unique.

Le webhook ne route plus : il transmet (phone, text) à l'Orchestrator via
Celery. C'est le WorkspaceResolver qui décide l'agent, de façon collante.
"""

import asyncio
import logging
from typing import Optional

import redis
from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request, Response

from ladini.api.security import verify_twilio_signature
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

# `dependencies=` au niveau du ROUTER (et non par route) : toute route ajoutée
# ici plus tard hérite de la validation de signature, au lieu de dépendre du
# fait que quelqu'un pense à la recopier.
#
# ⚠️ AUDIT SÉCURITÉ 2026-09-10 — cette dépendance MANQUAIT. `verify_twilio_signature`
# existait bien dans `api/security.py` (et `api/README.md` affirmait qu'elle
# « intercepte la requête pour valider l'authenticité de Twilio »), mais elle
# n'était câblée sur AUCUNE route : `POST /api/webhook/twilio` acceptait donc
# n'importe quel `From=whatsapp:+…`/`Body` de n'importe quel appelant. Comme
# tout ce qui suit dérive l'identité de l'utilisateur du seul champ `From`,
# c'était une usurpation d'identité complète — passer/confirmer une commande,
# désigner le gagnant d'une enchère, vider un stock, déclencher le
# téléchargement d'une URL arbitraire via `MediaUrl0` — pour quiconque
# connaissait l'URL du webhook. Ne pas retirer.
router = APIRouter(dependencies=[Depends(verify_twilio_signature)])
logger = logging.getLogger("Ladini.TwilioWebhook")


def _empty_twiml() -> Response:
    """Renvoie une réponse TwiML vide valide (text/xml) requise par Twilio."""
    return Response(content="<Response></Response>", media_type="application/xml")


# Statuts de livraison sortants à ignorer (callbacks de statut)
_DELIVERY_STATUSES = frozenset(
    {
        "accepted",
        "queued",
        "sending",
        "sent",
        "delivered",
        "read",
        "undelivered",
        "failed",
        "canceled",
    }
)

# (2026-09-17, follow-up pre-Hetzner) — voir whatsapp_webhook.py, même
# constante/rationale : borne dure sur `.delay()` pendant une panne broker.
_CELERY_DELAY_TIMEOUT_S = 5.0

# Initialisation du client Redis
redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)


def _extract_interactive_id(form: dict) -> Optional[str]:
    """Extrait le payload d'un clic WhatsApp (bouton quick-reply / ligne de liste)."""
    for key in ("ButtonPayload", "ListId", "SelectedId", "ListReplyId"):
        val = form.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def _extract_media(form: dict) -> Optional[tuple[str, str]]:
    """Extrait une image entrante WhatsApp (`MediaUrl0`/`MediaContentType0`).

    Renvoie ``(media_url, media_content_type)`` uniquement pour un média de
    type image (`NumMedia` > 0, `MediaUrl0` présent, `MediaContentType0`
    commençant par ``image/``) ; ``None`` sinon — audio/vidéo/document ou
    message texte classique. Fonction pure et déterministe : aucun accès
    réseau/DB, ne déclenche jamais rien elle-même — ne fait qu'extraire.

    NB : le champ Twilio réel est `MediaContentType{N}` (PAS `MimeType{N}`,
    qui est la convention Meta Cloud API — confusion corrigée après un bug
    en prod où une photo tombait silencieusement dans le pipeline texte).
    `MimeType0` reste lu en repli au cas où un intermédiaire le réécrirait.
    """
    num_media_raw = str(form.get("NumMedia") or "")
    num_media = int(num_media_raw) if num_media_raw.isdigit() else 0
    media_url = form.get("MediaUrl0")
    media_content_type = (
        str(form.get("MediaContentType0") or form.get("MimeType0") or "")
        .strip()
        .lower()
    )
    if num_media > 0 and media_url and media_content_type.startswith("image/"):
        return str(media_url), media_content_type
    return None


_VIEW_PHOTOS_PREFIXES = ("photos ", "photo ")


def _extract_view_photos_query(text: str) -> Optional[str]:
    """Commande déterministe « photos <nom> » / « photo <nom> » — renvoie le
    nom de produit demandé, ou ``None`` si le texte ne matche pas ce format.

    Volontairement un mot-clé simple (pas une intention LLM) : « photo(s) »
    n'est utilisé nulle part ailleurs dans l'interpréteur market_coach
    (vérifié), donc aucun risque de collision avec le pipeline texte normal.
    """
    stripped = text.strip()
    lowered = stripped.lower()
    for prefix in _VIEW_PHOTOS_PREFIXES:
        if lowered.startswith(prefix):
            query = stripped[len(prefix) :].strip()
            return query or None
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
        logger.warning(
            "TWILIO_WEBHOOK_LOCATION_INVALID | lat=%r | lon=%r", raw_lat, raw_lon
        )
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        logger.warning(
            "TWILIO_WEBHOOK_LOCATION_OUT_OF_RANGE | lat=%s | lon=%s", lat, lon
        )
        return None
    address = form.get("Address") or form.get("Label") or None
    return lat, lon, address


# (2026-09-02) L'ancienne persistance en `BackgroundTasks` (best-effort,
# APRÈS la réponse HTTP, sans garantie d'ordre vis-à-vis de la tâche Celery
# déjà enqueuée) a été retirée — voir `core/location.py::persist_shared_location`,
# maintenant appelée SYNCHRONE dans le handler ci-dessous, AVANT l'enqueue.


@router.post("/webhook/twilio")
async def twilio_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    From: str = Form(...),
    Body: str = Form(""),
    MessageSid: str = Form(...),
):
    try:
        return await _handle_twilio_webhook(
            request, background_tasks, From, Body, MessageSid
        )
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
    # `redis_client` est SYNCHRONE — voir la note détaillée dans
    # whatsapp_webhook.py (même correctif, même cause : un appel Redis
    # bloquant direct dans un handler async gèle la boucle asyncio, mesuré
    # via k6 comme le premier goulot d'étranglement de toute la stack).
    redis_key = f"msg:{MessageSid}"
    try:
        # Renvoie True uniquement si la clé n'existait pas (nouveau message)
        is_new_message = await asyncio.to_thread(
            redis_client.set, redis_key, "processing", ex=3600, nx=True
        )
        if not is_new_message:
            logger.warning(
                "TWILIO_WEBHOOK_DUPLICATE | MessageSid=%s déjà en cours ou traité",
                MessageSid,
            )
            return _empty_twiml()
    except Exception as e:
        # Si Redis flanche, on logue l'erreur mais on laisse passer le message (résilience)
        logger.error(
            "TWILIO_WEBHOOK_REDIS_ERROR | Impossible de vérifier l'idempotence: %s", e
        )

    # --- 2. NETTOYAGE DU NUMÉRO DE TÉLÉPHONE ---
    raw_sender = From
    phone = raw_sender.replace("whatsapp:", "").strip()
    if not phone:
        logger.warning("TWILIO_WEBHOOK_INVALID_SENDER | From=%r", From)
        return _empty_twiml()

    text = Body.strip()
    form = dict(await request.form())

    # Diagnostic média — volontairement TOUJOURS logué (pas seulement quand
    # une image est détectée) : c'est justement l'absence/la forme inattendue
    # de ces 3 champs qui doit se voir si `_extract_media` ne matche pas alors
    # qu'une photo a bien été envoyée. Champs non sensibles (métadonnées
    # Twilio, pas de contenu utilisateur).
    logger.info(
        "TWILIO_WEBHOOK_MEDIA_FIELDS | NumMedia=%r | MediaUrl0=%r | MediaContentType0=%r | MimeType0=%r",
        form.get("NumMedia"),
        form.get("MediaUrl0"),
        form.get("MediaContentType0"),
        form.get("MimeType0"),
    )

    # --- 3. FILTRAGE DES CALLBACKS DE STATUT ---
    status_value = (
        (form.get("MessageStatus") or form.get("SmsStatus") or "").strip().lower()
    )
    if status_value in _DELIVERY_STATUSES:
        logger.info(
            "TWILIO_STATUS_CALLBACK ignoré | sid=%s | status=%s",
            MessageSid,
            status_value,
        )
        return _empty_twiml()

    # --- 4. EXTRACTION INTERACTIVE & TÉLÉMÉTRIE ---
    interactive_id = _extract_interactive_id(form)
    if interactive_id:
        logger.info(
            "TWILIO_INBOUND_INTERACTIVE | phone=%s | id=%s | form_keys=%s",
            phone,
            interactive_id,
            list(form.keys()),
        )

    # --- 4bis. CAPTURE GPS NATIVE (message de localisation WhatsApp) ---
    # (2026-09-02, refonte GPS ; nettoyage architectural final 2026-09-02 —
    # "un seul propriétaire de la réponse") : le webhook ne fait plus QUE
    # normaliser l'événement entrant et persister le fait brut
    # (`persist_shared_location`, une ÉCRITURE, pas une décision de réponse).
    # Il ne calcule, ne choisit, ni n'envoie plus AUCUN message utilisateur
    # lié au GPS — l'ancienne notification immédiate ("hors de notre zone")
    # a été supprimée : elle faisait du webhook un 2e propriétaire de la
    # réponse, concurrent du tour de graphe normal, ce qui produisait
    # EXACTEMENT le double message que la garde `pending_interaction_kind`
    # rustinait localement sans traiter la cause. `location_outcome` est
    # transmis tel quel au tour de graphe ; c'est CE tour, et lui seul, qui
    # décide s'il y a quelque chose à dire (voir `nodes/clarification.py`
    # pour le cas "aucune étape GPS active" — le seul cas où rien d'autre
    # dans le graphe n'aurait autrement mentionné ce rejet).
    location = _extract_location(form)
    location_shared = False
    location_outcome: Optional[str] = None
    location_lat: Optional[float] = None
    location_lon: Optional[float] = None
    if location:
        lat, lon, address = location
        logger.info(
            "TWILIO_INBOUND_LOCATION | phone=%s | lat=%s | lon=%s | address=%r",
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

        telemetry.count_webhook("twilio")
        trace_id = getattr(request.state, "trace_id", None) or telemetry.new_trace_id()
    except Exception:
        trace_id = None

    # --- 4ter. PHOTO PRODUIT (MEDIA) ---
    # Une photo n'est pas un texte à interpréter par le LLM — routée vers une
    # tâche Celery dédiée, complètement DÉCOUPLÉE du pipeline agent existant :
    # ce bloc ne s'exécute QUE si `MediaUrl0` est présent, donc zéro impact
    # sur le flux texte habituel. Voir workers/media/product_photo_task.py.
    media = _extract_media(form)
    if media:
        media_url, media_content_type = media
        logger.info(
            "TWILIO_INBOUND_MEDIA | phone=%s | content_type=%s",
            phone,
            media_content_type,
        )
        # `.delay()` (Redis/kombu, synchrone) — asyncio.to_thread sur tous
        # les sites de ce fichier, même rationale que process_agent_task
        # ci-dessous.
        await asyncio.to_thread(
            process_product_photo_task.delay,
            phone_number=phone,
            media_url=media_url,
            media_content_type=media_content_type,
            trace_id=trace_id,
            message_sid=MessageSid,
        )
        return _empty_twiml()

    # --- 4quater. RÉPONSE À UN MENU "QUEL PRODUIT ?" EN ATTENTE ---
    # Court-circuite le pipeline agent UNIQUEMENT si (a) une photo est en
    # attente de sélection pour ce numéro ET (b) le texte est un simple
    # chiffre — sinon le flux texte normal continue sans aucun changement.
    # Compromis assumé : si l'utilisateur a AUSSI un autre menu numéroté
    # ouvert par ailleurs (cas rare), "1" sera pris pour la photo — la
    # fenêtre est courte (10 min) et ce cas suppose deux conversations
    # numérotées strictement simultanées.
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
                phone_number=phone, selection_text=text, message_sid=MessageSid
            )
            return _empty_twiml()

        # Même principe pour la désambiguïsation côté CONSULTATION (plusieurs
        # lots du même nom, ex: "maïs" publié 3 fois avec des quantités
        # différentes) — clé Redis distincte, donc pas de collision avec le
        # menu d'upload ci-dessus (celui-ci est vérifié en premier par
        # construction : un numéro ne peut résoudre qu'UN SEUL des deux).
        try:
            has_pending_view = bool(
                await asyncio.to_thread(redis_client.exists, pending_view_key(phone))
            )
        except Exception:
            has_pending_view = False
        if has_pending_view:
            await asyncio.to_thread(
                resolve_pending_view_photos_task.delay,
                phone_number=phone, selection_text=text, message_sid=MessageSid
            )
            return _empty_twiml()

    # --- 4quinquies. CONSULTATION "PHOTOS <NOM>" / "PHOTOS <NUMÉRO>" ---
    # Commande déterministe (mot-clé, pas une intention LLM) — voir
    # `_extract_view_photos_query`. Découplée du pipeline agent au même titre
    # que l'upload : c'est une consultation, pas une conversation.
    #
    # Deux cibles possibles, distinguées par la FORME de ce qui suit
    # "photos " — jamais d'ambiguïté puisqu'un nom de produit n'est jamais un
    # chiffre pur :
    #   - "photos <numéro>" -> résultat de recherche ACHETEUR (le numéro
    #     affiché par search_products, voir services/search_results_cache.py) ;
    #   - "photos <nom>"    -> catalogue PRODUCTEUR (ses propres produits).
    view_query = _extract_view_photos_query(text)
    if view_query:
        logger.info(
            "TWILIO_INBOUND_VIEW_PHOTOS | phone=%s | query=%r", phone, view_query
        )
        if view_query.isdigit():
            await asyncio.to_thread(
                send_search_result_photos_task.delay,
                phone_number=phone, index_text=view_query, message_sid=MessageSid
            )
        else:
            await asyncio.to_thread(
                send_product_photos_task.delay,
                phone_number=phone, product_query=view_query, message_sid=MessageSid
            )
        return _empty_twiml()

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

    # (2026-09-13, incident WhatsApp #3 — utilisateur double-rôle) : CE webhook
    # (pas seulement `orchestrator.py::_run_market`) fige déjà `force_role=True`
    # dès qu'un workspace existe — ce qui court-circuitait TOTALEMENT le
    # correctif posé dans l'orchestrateur (il ne s'exécute jamais quand
    # `force_role` est déjà True). `ws_type` ci-dessus est le dernier rôle
    # utilisé dans CE workspace (sticky, même numéro pour buyer/producteur) —
    # potentiellement périmé pour un producteur qui répond "confirmer"/
    # "annuler" à une notification de commande reçue alors que son workspace
    # est resté en mode BUYER. `flows/buyer/preorder_confirmation.py` pose un
    # indice d'ÉTAT plus récent (`pending_role_hint:{phone}`) juste après avoir
    # mis CETTE notification producteur en file — on le lit et le consomme ICI,
    # AVANT de figer le rôle, plutôt que de laisser l'orchestrateur tenter (en
    # vain) de le faire après coup. Fail-open comme tout `core/idempotency.py`.
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
        "Twilio webhook | phone=%s | text=%r | existing_workspace=%s",
        phone,
        text[:80],
        bool(workspace),
    )

    # --- 6. DÉLÉGATION À CELERY ---
    # (2026-09-20, bascule Upstash → Valkey) — vérifié JUSTE AVANT l'enqueue,
    # jamais plus tôt (voir core/maintenance.py) : le reste du traitement
    # ci-dessus (persistance GPS, résolution de rôle) reste idempotent et
    # s'exécute normalement, seul le PRODUCTEUR est gardé. 503 plutôt que
    # `_empty_twiml()` : Twilio redélivre ce webhook après un échec — le
    # message n'est PAS perdu, seulement retardé jusqu'à la fin de la
    # bascule.
    if is_celery_producer_paused():
        logger.info(
            "TWILIO_WEBHOOK_PAUSED | phone=%s | message_sid=%s — maintenance broker en cours",
            phone,
            MessageSid,
        )
        return Response(content=MAINTENANCE_MESSAGE, media_type="text/plain", status_code=503)

    # Borne dure (`asyncio.wait_for`) — voir la même note détaillée dans
    # whatsapp_webhook.py : `asyncio.to_thread` seul ne suffisait pas,
    # confirmé en E2E local (2026-09-17), un webhook restait bloqué
    # plusieurs dizaines de secondes pendant une panne Redis/broker malgré
    # le tuning kombu (celery_app.py). Le thread sous-jacent peut continuer
    # en arrière-plan sans bloquer CETTE requête au-delà du timeout.
    await asyncio.wait_for(
        asyncio.to_thread(
            process_agent_task.delay,
            phone_number=phone,
            user_query=text,
            workspace_type=ws_type,
            role=resolved_role,
            force_role=force_role,
            interactive_id=interactive_id,  # Bypass LLM si clic bouton/liste
            trace_id=trace_id,  # Propagation de la trace d'observabilité
            location_shared=location_shared,  # Position GPS reçue ce tour (accusé onboarding)
            # (2026-09-02) Issue EXACTE de la persistance (déjà terminée, ci-dessus,
            # avant cet enqueue) — l'agent n'a plus besoin de relire la DB pour
            # savoir si le point a été accepté/rejeté/en erreur. Voir
            # core/location.py et gps_delivery_gate.py::resolve_gps_stage.
            location_outcome=location_outcome,
            location_lat=location_lat,
            location_lon=location_lon,
            # (2026-09-02) Clé d'idempotence de l'ENVOI — voir api/tasks.py::
            # _claim_single_response. Même identifiant que la clé de
            # dédoublonnage webhook ci-dessus (bloc 1), réutilisé pour protéger
            # aussi contre un retry Celery de la tâche elle-même.
            message_sid=MessageSid,
        ),
        timeout=_CELERY_DELAY_TIMEOUT_S,
    )

    return _empty_twiml()
