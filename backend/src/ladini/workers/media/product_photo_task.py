"""Traitement asynchrone d'une photo produit reçue par WhatsApp.

Flux : téléchargement (Twilio Basic Auth via `media_url`, OU WhatsApp Cloud
API Bearer token via `media_id` — voir `api/routes/twilio_webhook.py` et
`api/routes/whatsapp_webhook.py` pour les deux points d'entrée, `_process`
choisit le téléchargeur selon lequel des deux champs est renseigné) ->
upload Supabase Storage -> résolution du produit cible (auto si non ambigu,
sinon menu WhatsApp + attente de la réponse) -> liaison en base ->
confirmation WhatsApp.

Volontairement DÉCOUPLÉ de ``process_agent_task``/LangGraph : une photo n'est
pas un texte à interpréter par le LLM, et ce module ne doit jamais ralentir
ni risquer de régresser le pipeline conversationnel existant. Même pattern
d'exécution que les crons (``workers/runtime.py::run_async``/``worker_session``,
voir ``workers/crons/order_expiry.py``).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import redis
from rapidfuzz import fuzz

from ladini.api.celery_app import celery_app
from ladini.core.formatting import fmt_num
from ladini.core.settings import settings
from ladini.workers.runtime import run_async, worker_session

logger = logging.getLogger("Ladini.Workers.ProductPhoto")

# Fenêtre pour répondre au menu "à quel produit correspond cette photo ?"
# avant que la sélection expire (cohérent avec l'idempotence webhook, qui
# vit 3600s — ici volontairement plus court : une sélection de produit est
# une action ponctuelle, pas un état à conserver longtemps).
_PENDING_TTL_SECONDS = 600
_MAX_MENU_CANDIDATES = 9  # un menu à un chiffre reste répondable par SMS/WhatsApp

_PENDING_KEY_PREFIX = "pending_photo:"
_PENDING_VIEW_KEY_PREFIX = "pending_view_photos:"
_NAME_MATCH_THRESHOLD = 60  # même échelle rapidfuzz (0-100) que auction.py

_redis_client: Optional["redis.Redis"] = None


def _redis() -> "redis.Redis":
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_client


def pending_photo_key(phone: str) -> str:
    """Clé Redis partagée avec les DEUX webhooks (twilio_webhook.py ET
    whatsapp_webhook.py, keyée uniquement par numéro — provider-agnostic) —
    DOIT rester identique partout pour que l'interception de la réponse
    numérique fonctionne quel que soit le canal entrant."""
    return f"{_PENDING_KEY_PREFIX}{phone}"


def pending_view_key(phone: str) -> str:
    """Même principe que `pending_photo_key`, pour la désambiguïsation côté
    CONSULTATION (« photos <nom> » qui matche plusieurs lots du même nom) —
    clé distincte pour ne jamais confondre les deux files d'attente."""
    return f"{_PENDING_VIEW_KEY_PREFIX}{phone}"


def _mask(phone: str) -> str:
    p = str(phone or "")
    return f"***{p[-4:]}" if len(p) >= 4 else "***"


def _format_candidate_label(index: int, product: Dict[str, Any]) -> str:
    """Libellé de menu numéroté — inclut la quantité/unité pour distinguer
    plusieurs lots du MÊME nom (ex: un producteur qui publie "maïs" trois
    fois avec des quantités différentes — 300kg, puis 245kg, puis 456kg — se
    retrouverait sinon avec un menu "1. maïs / 2. maïs / 3. maïs"
    strictement illisible, constaté après coup)."""
    name = product.get("name") or "Produit"
    qty = product.get("quantity_for_sale")
    unit = str(product.get("unit") or "").strip()
    qty_str = fmt_num(qty) if qty not in (None, "") else ""
    if qty_str:
        return f"{index + 1}. {name} ({qty_str} {unit})".rstrip()
    return f"{index + 1}. {name}"


async def _resolve_target_product(phone: str) -> Dict[str, Any]:
    """Détermine le produit cible d'une photo.

    Renvoie exactement une des trois formes :
      {"none": True}                       -- aucun produit publié
      {"resolved": <product dict>}         -- cible non ambiguë (un seul produit actif)
      {"ambiguous": [<product dict>, ...]} -- plusieurs candidats, il faut demander

    Pas d'heuristique "produit récemment modifié" : `add_product_photo`
    touche `updated_at` du produit qu'il vient de lier — une telle heuristique
    se contamine donc elle-même après la toute première photo (le produit
    choisi devient "le plus récent" et absorbe silencieusement TOUTES les
    photos suivantes, même sans rapport). Constaté en usage réel : mieux vaut
    redemander à chaque fois qu'un producteur a plusieurs produits actifs.
    """
    from ladini.services.database.d import AgriDatabaseService

    result = await AgriDatabaseService().get_my_products(phone)
    products: List[Dict[str, Any]] = (result or {}).get("data") or []
    if not products:
        return {"none": True}

    active = [p for p in products if p.get("is_available")]
    candidates = active or products
    if len(candidates) == 1:
        return {"resolved": candidates[0]}

    return {"ambiguous": candidates[:_MAX_MENU_CANDIDATES]}


async def _link_photo_and_confirm(
    phone: str,
    product: Dict[str, Any],
    image_url: str,
    *,
    message_sid: Optional[str] = None,
) -> None:
    from ladini.api.tasks import send_confirmation_text
    from ladini.services.database.d import AgriDatabaseService

    async with worker_session():
        result = await AgriDatabaseService().add_product_photo(
            phone=phone,
            product_id=str(product.get("id")),
            image_url=image_url,
        )

    if str(result.get("status")) == "success":
        name = (
            (result.get("data") or {}).get("name")
            or product.get("name")
            or "votre produit"
        )
        await send_confirmation_text(
            phone, f"✅ Photo ajoutée à *{name}*.", message_sid=message_sid
        )
    else:
        await send_confirmation_text(
            phone,
            f"❌ {result.get('message') or 'Échec de la liaison de la photo.'}",
            message_sid=message_sid,
        )


async def _ask_or_accumulate(
    phone: str,
    candidates: List[Dict[str, Any]],
    image_url: str,
    *,
    message_sid: Optional[str] = None,
) -> None:
    """Pose la question "quel produit ?" — UNE seule fois par rafale.

    WhatsApp/Twilio livre chaque photo d'un envoi groupé comme un message
    (donc un appel webhook) SÉPARÉ. Sans ce regroupement, un producteur qui
    envoie 3 photos du même article recevait 3 fois la même question — pas
    intuitif, et pénible à gérer même en y répondant correctement (constaté
    en usage réel). Si une question est déjà en attente pour ce numéro, la
    nouvelle photo est simplement ajoutée à la même file (`image_urls`) SANS
    renvoyer le menu ; la réponse numérique unique liera ensuite TOUTES les
    photos accumulées d'un coup (voir `_resolve_pending`).

    Compromis assumé : si le producteur veut en réalité répartir les photos
    d'une même rafale sur PLUSIEURS produits différents, ce n'est pas
    possible ici — il doit répondre à la question, puis renvoyer séparément
    les photos du second produit. Cas jugé rare face au cas courant (toutes
    les photos d'une rafale sont pour le même article).
    """
    from ladini.api.tasks import send_confirmation_text

    key = pending_photo_key(phone)
    existing_raw = _redis().get(key)
    if existing_raw:
        try:
            existing = json.loads(existing_raw)
        except (TypeError, ValueError):
            existing = None
        if existing and isinstance(existing.get("image_urls"), list):
            existing["image_urls"].append(image_url)
            _redis().setex(key, _PENDING_TTL_SECONDS, json.dumps(existing))
            logger.info(
                "PRODUCT_PHOTO_BATCH_APPENDED | phone=%s | count=%d",
                _mask(phone),
                len(existing["image_urls"]),
            )
            return  # la question a déjà été posée pour cette rafale

    lines = [_format_candidate_label(i, c) for i, c in enumerate(candidates)]
    _redis().setex(
        key,
        _PENDING_TTL_SECONDS,
        json.dumps(
            {
                "image_urls": [image_url],
                "product_ids": [str(c.get("id")) for c in candidates],
            }
        ),
    )
    text = (
        "📸 Vous avez plusieurs produits actifs. À quel produit correspond cette photo ?\n"
        + "\n".join(lines)
        + "\n\nSi vous envoyez d'autres photos du même produit, ne répondez qu'une seule fois "
        "à la fin — elles seront toutes liées ensemble.\n"
        "Répondez avec le numéro correspondant (ex: 1)."
    )
    await send_confirmation_text(phone, text, message_sid=message_sid)


async def _link_bid_photo_and_confirm(
    phone_number: str,
    bid_id: str,
    image_url: str,
    *,
    message_sid: Optional[str] = None,
) -> None:
    from ladini.api.tasks import send_confirmation_text
    from ladini.services.database.d import AgriDatabaseService

    async with worker_session():
        result = await AgriDatabaseService().add_bid_photo(
            phone=phone_number,
            bid_id=bid_id,
            image_url=image_url,
        )
    if str(result.get("status")) == "success":
        await send_confirmation_text(
            phone_number, "✅ Photo ajoutée à votre offre.", message_sid=message_sid
        )
    else:
        await send_confirmation_text(
            phone_number,
            f"❌ {result.get('message') or 'Échec de la liaison de la photo.'}",
            message_sid=message_sid,
        )


async def _link_auction_photo_and_confirm(
    phone_number: str,
    auction_id: str,
    image_url: str,
    *,
    message_sid: Optional[str] = None,
) -> None:
    from ladini.api.tasks import send_confirmation_text
    from ladini.services.database.d import AgriDatabaseService

    async with worker_session():
        result = await AgriDatabaseService().add_auction_photo(
            phone=phone_number,
            auction_id=auction_id,
            image_url=image_url,
        )
    if str(result.get("status")) == "success":
        await send_confirmation_text(
            phone_number,
            "✅ Photo ajoutée à votre appel d'offres.",
            message_sid=message_sid,
        )
    else:
        await send_confirmation_text(
            phone_number,
            f"❌ {result.get('message') or 'Échec de la liaison de la photo.'}",
            message_sid=message_sid,
        )


async def _process(
    phone_number: str,
    media_url: str,
    media_content_type: str,
    *,
    media_id: str = "",
    message_sid: Optional[str] = None,
) -> None:
    from ladini.api.tasks import send_confirmation_text
    from ladini.services.pending_photo_target import (
        pop_pending_auction_photo,
        pop_pending_bid_photo,
        pop_pending_product_photo,
    )
    from ladini.services.storage.supabase_storage import (
        SupabaseStorageError,
        upload_product_photo,
    )
    from ladini.services.whatsapp.cloud_api_media import (
        CloudAPIMediaError,
        download_cloud_api_media,
    )
    from ladini.services.whatsapp.twilio_media import (
        TwilioMediaError,
        download_twilio_media,
    )

    # Deux fournisseurs possibles, jamais les deux à la fois (voir
    # `api/routes/whatsapp_webhook.py` — Meta Cloud API ne donne qu'un `id`
    # opaque, jamais une URL directe fetchable — vs. `api/routes/
    # twilio_webhook.py` — `MediaUrl0`). `media_id` a priorité : c'est le
    # SEUL champ que le webhook Cloud API peut renseigner.
    try:
        if media_id:
            binary, resolved_content_type = await download_cloud_api_media(media_id)
        else:
            binary, resolved_content_type = await download_twilio_media(media_url)
    except (TwilioMediaError, CloudAPIMediaError) as exc:
        logger.warning(
            "PRODUCT_PHOTO_DOWNLOAD_FAILED | phone=%s | %s", _mask(phone_number), exc
        )
        await send_confirmation_text(phone_number, f"❌ {exc}", message_sid=message_sid)
        return

    content_type = (media_content_type or resolved_content_type or "").strip().lower()

    try:
        public_url = await upload_product_photo(binary, content_type, phone_number)
    except SupabaseStorageError as exc:
        logger.warning(
            "PRODUCT_PHOTO_UPLOAD_FAILED | phone=%s | %s", _mask(phone_number), exc
        )
        await send_confirmation_text(phone_number, f"❌ {exc}", message_sid=message_sid)
        return

    # Priorité : une offre/un appel d'offres/un PRODUIT tout juste créé(e)
    # absorbe la PROCHAINE photo envoyée (voir services/pending_photo_target.py,
    # posé par nodes/rendering/success.py juste après
    # place_bid/create_auction/create_product). Bid avant auction avant
    # produit (ordre arbitraire mais documenté) : un même numéro ne devrait
    # avoir qu'UN SEUL marqueur actif en pratique (une seule action récente
    # à la fois). Aucun marqueur → repli sur le catalogue produit du
    # producteur (comportement historique inchangé, `_resolve_target_product`).
    pending_bid_id = pop_pending_bid_photo(phone_number)
    if pending_bid_id:
        await _link_bid_photo_and_confirm(
            phone_number, pending_bid_id, public_url, message_sid=message_sid
        )
        return

    pending_auction_id = pop_pending_auction_photo(phone_number)
    if pending_auction_id:
        await _link_auction_photo_and_confirm(
            phone_number, pending_auction_id, public_url, message_sid=message_sid
        )
        return

    pending_product_id = pop_pending_product_photo(phone_number)
    if pending_product_id:
        await _link_photo_and_confirm(
            phone_number,
            {"id": pending_product_id},
            public_url,
            message_sid=message_sid,
        )
        return

    async with worker_session():
        resolution = await _resolve_target_product(phone_number)

    if resolution.get("none"):
        await send_confirmation_text(
            phone_number,
            "Vous n'avez encore aucun produit publié — publiez un produit avant d'y ajouter une photo.",
            message_sid=message_sid,
        )
        return

    if "resolved" in resolution:
        await _link_photo_and_confirm(
            phone_number, resolution["resolved"], public_url, message_sid=message_sid
        )
        return

    await _ask_or_accumulate(
        phone_number,
        resolution.get("ambiguous") or [],
        public_url,
        message_sid=message_sid,
    )


async def _resolve_pending(
    phone_number: str, selection_text: str, *, message_sid: Optional[str] = None
) -> None:
    from ladini.api.tasks import send_confirmation_text

    key = pending_photo_key(phone_number)
    raw = _redis().get(key)
    if not raw:
        await send_confirmation_text(
            phone_number,
            "Cette sélection a expiré, veuillez renvoyer la photo.",
            message_sid=message_sid,
        )
        return

    try:
        pending = json.loads(raw)
    except (TypeError, ValueError):
        _redis().delete(key)
        logger.warning("PRODUCT_PHOTO_PENDING_CORRUPT | phone=%s", _mask(phone_number))
        return

    try:
        idx = int(str(selection_text).strip()) - 1
    except ValueError:
        return

    product_ids: List[str] = pending.get("product_ids") or []
    # Compat rétro : d'anciennes entrées Redis encore en TTL au moment du
    # déploiement de ce fix peuvent porter l'ancien format mono-photo.
    image_urls: List[str] = pending.get("image_urls") or (
        [pending["image_url"]] if pending.get("image_url") else []
    )
    if idx < 0 or idx >= len(product_ids) or not image_urls:
        await send_confirmation_text(
            phone_number, "Numéro invalide, veuillez réessayer.", message_sid=message_sid
        )
        return

    product_id = product_ids[idx]
    _redis().delete(key)

    from ladini.services.database.d import AgriDatabaseService

    linked = 0
    name = "votre produit"
    last_error = ""
    async with worker_session():
        for url in image_urls:
            result = await AgriDatabaseService().add_product_photo(
                phone=phone_number,
                product_id=product_id,
                image_url=url,
            )
            if str(result.get("status")) == "success":
                linked += 1
                name = (result.get("data") or {}).get("name") or name
            else:
                last_error = result.get("message") or last_error

    if linked:
        plural = "s" if linked > 1 else ""
        await send_confirmation_text(
            phone_number,
            f"✅ {linked} photo{plural} ajoutée{plural} à *{name}*.",
            message_sid=message_sid,
        )
    else:
        await send_confirmation_text(
            phone_number,
            f"❌ {last_error or 'Échec de la liaison des photos.'}",
            message_sid=message_sid,
        )


@celery_app.task(
    bind=True,
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=5,
    retry_jitter=True,
)
def process_product_photo_task(
    self,
    phone_number: str = "",
    media_url: str = "",
    media_content_type: str = "",
    media_id: str = "",
    trace_id: Optional[str] = None,
    message_sid: Optional[str] = None,
) -> None:
    try:
        from ladini.core import telemetry

        telemetry.set_trace_context(trace_id, user_phone=phone_number)
    except Exception:
        telemetry = None  # type: ignore

    try:
        run_async(
            _process(
                phone_number,
                media_url,
                media_content_type,
                media_id=media_id,
                message_sid=message_sid,
            )
        )
    except Exception:
        logger.exception("PRODUCT_PHOTO_TASK_FAILED | phone=%s", _mask(phone_number))
        raise
    finally:
        if telemetry is not None:
            telemetry.flush()


@celery_app.task(bind=True, max_retries=2, autoretry_for=(Exception,), retry_backoff=5)
def resolve_pending_product_photo_task(
    self,
    phone_number: str = "",
    selection_text: str = "",
    message_sid: Optional[str] = None,
) -> None:
    try:
        run_async(_resolve_pending(phone_number, selection_text, message_sid=message_sid))
    except Exception:
        logger.exception("PRODUCT_PHOTO_RESOLVE_FAILED | phone=%s", _mask(phone_number))
        raise


# =====================================================================
# CONSULTATION : "photos <nom>" -- renvoie les photos d'un produit
# =====================================================================


async def _find_product_by_name(phone: str, query: str) -> Dict[str, Any]:
    """Résout un ou plusieurs produits du producteur par leur nom (floue, sur
    SES produits uniquement — liste courte, pas besoin d'une recherche
    trigram côté DB).

    Renvoie exactement une des quatre formes :
      {"none": True}                       -- aucun produit publié
      {"resolved": <product dict>}         -- une seule correspondance
      {"ambiguous": [<product dict>, ...]} -- plusieurs LOTS du même nom
                                               (ex: "maïs" publié 3 fois avec
                                               des quantités différentes —
                                               rien dans le nom seul ne les
                                               distingue, il faut demander)
      {"not_found": [<name>, ...]}         -- pas de correspondance suffisante
    """
    from ladini.services.database.d import AgriDatabaseService

    result = await AgriDatabaseService().get_my_products(phone)
    products: List[Dict[str, Any]] = (result or {}).get("data") or []
    if not products:
        return {"none": True}

    scored = sorted(
        ((fuzz.WRatio(query, str(p.get("name") or "")), p) for p in products),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score = scored[0][0] if scored else 0
    if best_score < _NAME_MATCH_THRESHOLD:
        return {"not_found": [str(p.get("name") or "") for _, p in scored]}

    top_matches = [p for score, p in scored if score == best_score]
    if len(top_matches) == 1:
        return {"resolved": top_matches[0]}
    return {"ambiguous": top_matches[:_MAX_MENU_CANDIDATES]}


async def _ask_which_batch_to_view(
    phone: str, candidates: List[Dict[str, Any]], *, message_sid: Optional[str] = None
) -> None:
    from ladini.api.tasks import send_confirmation_text

    lines = [_format_candidate_label(i, c) for i, c in enumerate(candidates)]
    _redis().setex(
        pending_view_key(phone),
        _PENDING_TTL_SECONDS,
        json.dumps({"product_ids": [str(c.get("id")) for c in candidates]}),
    )
    text = (
        "📦 Plusieurs lots correspondent à ce nom :\n"
        + "\n".join(lines)
        + "\n\nRépondez avec le numéro correspondant (ex: 1)."
    )
    await send_confirmation_text(phone, text, message_sid=message_sid)


def _photo_items_for_product(product: Dict[str, Any]):
    """Construit les `ImageResponse` (une par photo) pour un produit — PURE,
    aucun envoi. Séparée de `_send_photos_for_product` pour que les
    appelants qui doivent AUSSI ajouter un `TextResponse` complémentaire
    (ex: `_send_search_result_photos`) composent un SEUL `ResponsePlan`
    plutôt que deux dispatches indépendants dont les index d'idempotence
    (`event_id:0`, `event_id:1`...) collisionneraient sinon silencieusement
    (2026-09-02, bug trouvé et corrigé pendant cette consolidation)."""
    from ladini.api.response_dispatch import ImageResponse

    name = product.get("name") or "ce produit"
    images: List[str] = product.get("images") or []
    return tuple(
        ImageResponse(
            url=url,
            caption=(
                f"📸 {name} ({i + 1}/{len(images)})" if len(images) > 1 else f"📸 {name}"
            ),
        )
        for i, url in enumerate(images)
    )


async def _send_photos_for_product(
    phone_number: str, product: Dict[str, Any], *, message_sid: Optional[str] = None
) -> None:
    """(2026-09-02, consolidation finale — mandat §3) : le média passe
    désormais par le MÊME `ResponseDispatcher` que le texte — un
    `ResponsePlan` porte une `ImageResponse` PAR PHOTO, chacune avec sa
    propre clé d'idempotence (`event_id:index`, voir
    `api/response_dispatch.py::ResponseDispatcher.dispatch`) : un retry
    Celery après l'envoi de 2 photos sur 3 ne renvoie PAS les 2 déjà
    parties, et complète bien la 3e manquante."""
    from ladini.api.response_dispatch import ResponsePlan, get_dispatcher

    items = _photo_items_for_product(product)
    if not items:
        from ladini.api.tasks import send_confirmation_text

        name = product.get("name") or "ce produit"
        await send_confirmation_text(
            phone_number,
            f"📭 Aucune photo pour *{name}* pour le moment.",
            message_sid=message_sid,
        )
        return

    plan = ResponsePlan(event_id=message_sid, items=items)
    await get_dispatcher().dispatch(phone_number, plan)


async def _send_photos(
    phone_number: str, product_query: str, *, message_sid: Optional[str] = None
) -> None:
    from ladini.api.tasks import send_confirmation_text

    async with worker_session():
        resolution = await _find_product_by_name(phone_number, product_query)

    if resolution.get("none"):
        await send_confirmation_text(
            phone_number,
            "Vous n'avez encore aucun produit publié.",
            message_sid=message_sid,
        )
        return

    if "not_found" in resolution:
        names = resolution["not_found"]
        listing = "\n".join(f"- {n}" for n in names)
        await send_confirmation_text(
            phone_number,
            f"Produit « {product_query} » introuvable. Vos produits :\n{listing}",
            message_sid=message_sid,
        )
        return

    if "ambiguous" in resolution:
        await _ask_which_batch_to_view(
            phone_number, resolution["ambiguous"], message_sid=message_sid
        )
        return

    await _send_photos_for_product(
        phone_number, resolution["resolved"], message_sid=message_sid
    )


async def _resolve_pending_view(
    phone_number: str, selection_text: str, *, message_sid: Optional[str] = None
) -> None:
    from ladini.api.tasks import send_confirmation_text

    key = pending_view_key(phone_number)
    raw = _redis().get(key)
    if not raw:
        await send_confirmation_text(
            phone_number,
            "Cette sélection a expiré, retapez votre demande (ex: photos maïs).",
            message_sid=message_sid,
        )
        return

    try:
        pending = json.loads(raw)
    except (TypeError, ValueError):
        _redis().delete(key)
        logger.warning(
            "PRODUCT_PHOTO_VIEW_PENDING_CORRUPT | phone=%s", _mask(phone_number)
        )
        return

    try:
        idx = int(str(selection_text).strip()) - 1
    except ValueError:
        return

    product_ids: List[str] = pending.get("product_ids") or []
    if idx < 0 or idx >= len(product_ids):
        await send_confirmation_text(
            phone_number, "Numéro invalide, veuillez réessayer.", message_sid=message_sid
        )
        return

    product_id = product_ids[idx]
    _redis().delete(key)

    from ladini.services.database.d import AgriDatabaseService

    async with worker_session():
        result = await AgriDatabaseService().get_my_products(phone_number)
    products: List[Dict[str, Any]] = (result or {}).get("data") or []
    product = next((p for p in products if str(p.get("id")) == product_id), None)
    if not product:
        await send_confirmation_text(
            phone_number, "Ce produit n'existe plus.", message_sid=message_sid
        )
        return

    await _send_photos_for_product(phone_number, product, message_sid=message_sid)


@celery_app.task(bind=True, max_retries=2, autoretry_for=(Exception,), retry_backoff=5)
def send_product_photos_task(
    self,
    phone_number: str = "",
    product_query: str = "",
    message_sid: Optional[str] = None,
) -> None:
    try:
        run_async(_send_photos(phone_number, product_query, message_sid=message_sid))
    except Exception:
        logger.exception("PRODUCT_PHOTO_VIEW_FAILED | phone=%s", _mask(phone_number))
        raise


@celery_app.task(bind=True, max_retries=2, autoretry_for=(Exception,), retry_backoff=5)
def resolve_pending_view_photos_task(
    self,
    phone_number: str = "",
    selection_text: str = "",
    message_sid: Optional[str] = None,
) -> None:
    try:
        run_async(
            _resolve_pending_view(phone_number, selection_text, message_sid=message_sid)
        )
    except Exception:
        logger.exception(
            "PRODUCT_PHOTO_VIEW_RESOLVE_FAILED | phone=%s", _mask(phone_number)
        )
        raise


# =====================================================================
# CONSULTATION ACHETEUR : "photos <numéro>" -- résultat de recherche
# =====================================================================
# Distinct du bloc producteur ci-dessus : un acheteur qui cherche un produit
# (nœud search_products, voir nodes/rendering/success.py) voit une liste
# NUMÉROTÉE de résultats venant potentiellement de PLUSIEURS producteurs —
# jamais son propre catalogue. Le numéro affiché est mis en cache par le
# rendu (services/search_results_cache.py, SANS dépendance Celery) ; ce
# module le relit pour résoudre "photos <numéro>" en produit + photos.
# Distingué de la commande producteur "photos <nom>" côté webhook : un nom
# de produit n'est jamais un chiffre pur, donc aucune ambiguïté possible.


async def _send_search_result_photos(
    phone_number: str, index_text: str, *, message_sid: Optional[str] = None
) -> None:
    from ladini.api.tasks import send_confirmation_text
    from ladini.services.search_results_cache import load_results

    cached = load_results(phone_number)
    if not cached:
        await send_confirmation_text(
            phone_number,
            "Je n'ai plus votre dernière recherche sous la main — refaites une "
            "recherche puis retapez *photos <numéro>*.",
            message_sid=message_sid,
        )
        return

    index_clean = str(index_text).strip()
    entry = cached.get(index_clean)
    if not entry:
        await send_confirmation_text(
            phone_number, "Numéro invalide, veuillez réessayer.", message_sid=message_sid
        )
        return

    # (2026-09-02) UN SEUL `ResponsePlan` pour les photos + le rappel de
    # numéro — jamais deux dispatches indépendants pour le même événement
    # (chacun repartirait son propre index d'idempotence à 0, collisionnant
    # silencieusement — bug trouvé et corrigé pendant cette consolidation,
    # voir `_photo_items_for_product`). Rupture UX constatée en usage réel :
    # consulter une photo ne doit jamais faire perdre le fil d'une commande
    # en cours — ce message n'avance PAS la conversation (aucun état
    # LangGraph touché, `photos <numéro>` est entièrement hors-graphe, voir
    # docstring de module), donc le numéro affiché dans le menu d'origine
    # (ex: choix producteur) reste valable ; on le rappelle explicitement
    # pour que l'acheteur sache qu'il peut directement continuer.
    from ladini.api.response_dispatch import (
        ResponsePlan,
        TextResponse,
        get_dispatcher,
    )

    photo_items = _photo_items_for_product(entry)
    if not photo_items:
        # Même apology que `_send_photos_for_product` pour ce cas — voir sa
        # docstring : ce chemin ne passe PAS par elle (il compose SON PROPRE
        # ResponsePlan multipart), donc le comportement doit être reproduit
        # ici explicitement plutôt que silencieusement perdu.
        name = entry.get("name") or "ce produit"
        photo_items = (TextResponse(text=f"📭 Aucune photo pour *{name}* pour le moment."),)
    reminder = TextResponse(
        text=f"👆 Vous pouvez répondre *{index_clean}* pour continuer avec ce producteur."
    )
    plan = ResponsePlan(event_id=message_sid, items=(*photo_items, reminder))
    await get_dispatcher().dispatch(phone_number, plan)


@celery_app.task(bind=True, max_retries=2, autoretry_for=(Exception,), retry_backoff=5)
def send_search_result_photos_task(
    self,
    phone_number: str = "",
    index_text: str = "",
    message_sid: Optional[str] = None,
) -> None:
    try:
        run_async(
            _send_search_result_photos(phone_number, index_text, message_sid=message_sid)
        )
    except Exception:
        logger.exception("SEARCH_RESULT_PHOTO_FAILED | phone=%s", _mask(phone_number))
        raise
