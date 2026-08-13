"""Traitement asynchrone d'une photo produit reçue par WhatsApp.

Flux : téléchargement Twilio (Basic Auth) -> upload Supabase Storage ->
résolution du produit cible (auto si non ambigu, sinon menu WhatsApp + attente
de la réponse) -> liaison en base -> confirmation WhatsApp.

Volontairement DÉCOUPLÉ de ``process_agent_task``/LangGraph : une photo n'est
pas un texte à interpréter par le LLM, et ce module ne doit jamais ralentir
ni risquer de régresser le pipeline conversationnel existant. Même pattern
d'exécution que les crons (``workers/runtime.py::run_async``/``worker_session``,
voir ``workers/crons/order_expiry.py``).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import redis
from rapidfuzz import fuzz

from agriconnect.api.celery_app import celery_app
from agriconnect.core.formatting import fmt_num
from agriconnect.core.settings import settings
from agriconnect.workers.runtime import run_async, worker_session

logger = logging.getLogger("AgriConnect.Workers.ProductPhoto")

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
    """Clé Redis partagée avec le webhook (twilio_webhook.py) — DOIT rester
    identique des deux côtés pour que l'interception de la réponse numérique
    fonctionne."""
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
    from agriconnect.services.database.d import AgriDatabaseService

    result = await AgriDatabaseService().get_my_products(phone)
    products: List[Dict[str, Any]] = (result or {}).get("data") or []
    if not products:
        return {"none": True}

    active = [p for p in products if p.get("is_available")]
    candidates = active or products
    if len(candidates) == 1:
        return {"resolved": candidates[0]}

    return {"ambiguous": candidates[:_MAX_MENU_CANDIDATES]}


async def _link_photo_and_confirm(phone: str, product: Dict[str, Any], image_url: str) -> None:
    from agriconnect.api.tasks import send_confirmation_text
    from agriconnect.services.database.d import AgriDatabaseService

    async with worker_session():
        result = await AgriDatabaseService().add_product_photo(
            phone=phone, product_id=str(product.get("id")), image_url=image_url,
        )

    if str(result.get("status")) == "success":
        name = (result.get("data") or {}).get("name") or product.get("name") or "votre produit"
        await send_confirmation_text(phone, f"✅ Photo ajoutée à *{name}*.")
    else:
        await send_confirmation_text(
            phone, f"❌ {result.get('message') or 'Échec de la liaison de la photo.'}",
        )


async def _ask_or_accumulate(phone: str, candidates: List[Dict[str, Any]], image_url: str) -> None:
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
    from agriconnect.api.tasks import send_confirmation_text

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
                _mask(phone), len(existing["image_urls"]),
            )
            return  # la question a déjà été posée pour cette rafale

    lines = [_format_candidate_label(i, c) for i, c in enumerate(candidates)]
    _redis().setex(
        key,
        _PENDING_TTL_SECONDS,
        json.dumps({
            "image_urls": [image_url],
            "product_ids": [str(c.get("id")) for c in candidates],
        }),
    )
    text = (
        "📸 Vous avez plusieurs produits actifs. À quel produit correspond cette photo ?\n"
        + "\n".join(lines)
        + "\n\nSi vous envoyez d'autres photos du même produit, ne répondez qu'une seule fois "
        "à la fin — elles seront toutes liées ensemble.\n"
        "Répondez avec le numéro correspondant (ex: 1)."
    )
    await send_confirmation_text(phone, text)


async def _link_bid_photo_and_confirm(phone_number: str, bid_id: str, image_url: str) -> None:
    from agriconnect.api.tasks import send_confirmation_text
    from agriconnect.services.database.d import AgriDatabaseService

    async with worker_session():
        result = await AgriDatabaseService().add_bid_photo(
            phone=phone_number, bid_id=bid_id, image_url=image_url,
        )
    if str(result.get("status")) == "success":
        await send_confirmation_text(phone_number, "✅ Photo ajoutée à votre offre.")
    else:
        await send_confirmation_text(
            phone_number, f"❌ {result.get('message') or 'Échec de la liaison de la photo.'}",
        )


async def _link_auction_photo_and_confirm(phone_number: str, auction_id: str, image_url: str) -> None:
    from agriconnect.api.tasks import send_confirmation_text
    from agriconnect.services.database.d import AgriDatabaseService

    async with worker_session():
        result = await AgriDatabaseService().add_auction_photo(
            phone=phone_number, auction_id=auction_id, image_url=image_url,
        )
    if str(result.get("status")) == "success":
        await send_confirmation_text(phone_number, "✅ Photo ajoutée à votre appel d'offres.")
    else:
        await send_confirmation_text(
            phone_number, f"❌ {result.get('message') or 'Échec de la liaison de la photo.'}",
        )


async def _process(phone_number: str, media_url: str, media_content_type: str) -> None:
    from agriconnect.services.whatsapp.twilio_media import download_twilio_media, TwilioMediaError
    from agriconnect.services.storage.supabase_storage import upload_product_photo, SupabaseStorageError
    from agriconnect.services.pending_photo_target import pop_pending_bid_photo, pop_pending_auction_photo
    from agriconnect.api.tasks import send_confirmation_text

    try:
        binary, resolved_content_type = await download_twilio_media(media_url)
    except TwilioMediaError as exc:
        logger.warning("PRODUCT_PHOTO_DOWNLOAD_FAILED | phone=%s | %s", _mask(phone_number), exc)
        await send_confirmation_text(phone_number, f"❌ {exc}")
        return

    content_type = (media_content_type or resolved_content_type or "").strip().lower()

    try:
        public_url = await upload_product_photo(binary, content_type, phone_number)
    except SupabaseStorageError as exc:
        logger.warning("PRODUCT_PHOTO_UPLOAD_FAILED | phone=%s | %s", _mask(phone_number), exc)
        await send_confirmation_text(phone_number, f"❌ {exc}")
        return

    # Priorité : une offre/un appel d'offres tout juste créé(e) absorbe la
    # PROCHAINE photo envoyée (voir services/pending_photo_target.py, posé
    # par nodes/rendering/success.py juste après place_bid/create_auction).
    # Bid avant auction (ordre arbitraire mais documenté) : un même numéro ne
    # devrait avoir qu'UN SEUL marqueur actif en pratique (producteur XOR
    # acheteur pour la même action récente). Ni l'un ni l'autre → repli sur
    # le catalogue produit du producteur (comportement historique inchangé).
    pending_bid_id = pop_pending_bid_photo(phone_number)
    if pending_bid_id:
        await _link_bid_photo_and_confirm(phone_number, pending_bid_id, public_url)
        return

    pending_auction_id = pop_pending_auction_photo(phone_number)
    if pending_auction_id:
        await _link_auction_photo_and_confirm(phone_number, pending_auction_id, public_url)
        return

    async with worker_session():
        resolution = await _resolve_target_product(phone_number)

    if resolution.get("none"):
        await send_confirmation_text(
            phone_number,
            "Vous n'avez encore aucun produit publié — publiez un produit avant d'y ajouter une photo.",
        )
        return

    if "resolved" in resolution:
        await _link_photo_and_confirm(phone_number, resolution["resolved"], public_url)
        return

    await _ask_or_accumulate(phone_number, resolution.get("ambiguous") or [], public_url)


async def _resolve_pending(phone_number: str, selection_text: str) -> None:
    from agriconnect.api.tasks import send_confirmation_text

    key = pending_photo_key(phone_number)
    raw = _redis().get(key)
    if not raw:
        await send_confirmation_text(
            phone_number, "Cette sélection a expiré, veuillez renvoyer la photo.",
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
        await send_confirmation_text(phone_number, "Numéro invalide, veuillez réessayer.")
        return

    product_id = product_ids[idx]
    _redis().delete(key)

    from agriconnect.services.database.d import AgriDatabaseService
    linked = 0
    name = "votre produit"
    last_error = ""
    async with worker_session():
        for url in image_urls:
            result = await AgriDatabaseService().add_product_photo(
                phone=phone_number, product_id=product_id, image_url=url,
            )
            if str(result.get("status")) == "success":
                linked += 1
                name = (result.get("data") or {}).get("name") or name
            else:
                last_error = result.get("message") or last_error

    if linked:
        plural = "s" if linked > 1 else ""
        await send_confirmation_text(
            phone_number, f"✅ {linked} photo{plural} ajoutée{plural} à *{name}*.",
        )
    else:
        await send_confirmation_text(
            phone_number, f"❌ {last_error or 'Échec de la liaison des photos.'}",
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
    trace_id: Optional[str] = None,
) -> None:
    try:
        from agriconnect.core import telemetry
        telemetry.set_trace_context(trace_id, user_phone=phone_number)
    except Exception:
        telemetry = None  # type: ignore

    try:
        run_async(_process(phone_number, media_url, media_content_type))
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
) -> None:
    try:
        run_async(_resolve_pending(phone_number, selection_text))
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
    from agriconnect.services.database.d import AgriDatabaseService

    result = await AgriDatabaseService().get_my_products(phone)
    products: List[Dict[str, Any]] = (result or {}).get("data") or []
    if not products:
        return {"none": True}

    scored = sorted(
        (
            (fuzz.WRatio(query, str(p.get("name") or "")), p)
            for p in products
        ),
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


async def _ask_which_batch_to_view(phone: str, candidates: List[Dict[str, Any]]) -> None:
    from agriconnect.api.tasks import send_confirmation_text

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
    await send_confirmation_text(phone, text)


async def _send_photos_for_product(phone_number: str, product: Dict[str, Any]) -> None:
    from agriconnect.services.twilio_sender import send_whatsapp_media
    from agriconnect.api.tasks import send_confirmation_text

    name = product.get("name") or "ce produit"
    images: List[str] = product.get("images") or []
    if not images:
        await send_confirmation_text(phone_number, f"📭 Aucune photo pour *{name}* pour le moment.")
        return

    for i, url in enumerate(images):
        caption = f"📸 {name} ({i + 1}/{len(images)})" if len(images) > 1 else f"📸 {name}"
        # `send_whatsapp_media` est SYNC (SDK Twilio) — délestée sur un thread
        # pour ne jamais bloquer la boucle asyncio du worker, même pattern
        # que `_send_sync_twilio` dans workers/outbox/channels/whatsapp.py.
        await asyncio.to_thread(send_whatsapp_media, phone_number, url, caption)


async def _send_photos(phone_number: str, product_query: str) -> None:
    from agriconnect.api.tasks import send_confirmation_text

    async with worker_session():
        resolution = await _find_product_by_name(phone_number, product_query)

    if resolution.get("none"):
        await send_confirmation_text(
            phone_number, "Vous n'avez encore aucun produit publié.",
        )
        return

    if "not_found" in resolution:
        names = resolution["not_found"]
        listing = "\n".join(f"- {n}" for n in names)
        await send_confirmation_text(
            phone_number,
            f"Produit « {product_query} » introuvable. Vos produits :\n{listing}",
        )
        return

    if "ambiguous" in resolution:
        await _ask_which_batch_to_view(phone_number, resolution["ambiguous"])
        return

    await _send_photos_for_product(phone_number, resolution["resolved"])


async def _resolve_pending_view(phone_number: str, selection_text: str) -> None:
    from agriconnect.api.tasks import send_confirmation_text

    key = pending_view_key(phone_number)
    raw = _redis().get(key)
    if not raw:
        await send_confirmation_text(
            phone_number, "Cette sélection a expiré, retapez votre demande (ex: photos maïs).",
        )
        return

    try:
        pending = json.loads(raw)
    except (TypeError, ValueError):
        _redis().delete(key)
        logger.warning("PRODUCT_PHOTO_VIEW_PENDING_CORRUPT | phone=%s", _mask(phone_number))
        return

    try:
        idx = int(str(selection_text).strip()) - 1
    except ValueError:
        return

    product_ids: List[str] = pending.get("product_ids") or []
    if idx < 0 or idx >= len(product_ids):
        await send_confirmation_text(phone_number, "Numéro invalide, veuillez réessayer.")
        return

    product_id = product_ids[idx]
    _redis().delete(key)

    from agriconnect.services.database.d import AgriDatabaseService
    async with worker_session():
        result = await AgriDatabaseService().get_my_products(phone_number)
    products: List[Dict[str, Any]] = (result or {}).get("data") or []
    product = next((p for p in products if str(p.get("id")) == product_id), None)
    if not product:
        await send_confirmation_text(phone_number, "Ce produit n'existe plus.")
        return

    await _send_photos_for_product(phone_number, product)


@celery_app.task(bind=True, max_retries=2, autoretry_for=(Exception,), retry_backoff=5)
def send_product_photos_task(
    self,
    phone_number: str = "",
    product_query: str = "",
) -> None:
    try:
        run_async(_send_photos(phone_number, product_query))
    except Exception:
        logger.exception("PRODUCT_PHOTO_VIEW_FAILED | phone=%s", _mask(phone_number))
        raise


@celery_app.task(bind=True, max_retries=2, autoretry_for=(Exception,), retry_backoff=5)
def resolve_pending_view_photos_task(
    self,
    phone_number: str = "",
    selection_text: str = "",
) -> None:
    try:
        run_async(_resolve_pending_view(phone_number, selection_text))
    except Exception:
        logger.exception("PRODUCT_PHOTO_VIEW_RESOLVE_FAILED | phone=%s", _mask(phone_number))
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

async def _send_search_result_photos(phone_number: str, index_text: str) -> None:
    from agriconnect.api.tasks import send_confirmation_text
    from agriconnect.services.search_results_cache import load_results

    cached = load_results(phone_number)
    if not cached:
        await send_confirmation_text(
            phone_number,
            "Je n'ai plus votre dernière recherche sous la main — refaites une "
            "recherche puis retapez *photos <numéro>*.",
        )
        return

    index_clean = str(index_text).strip()
    entry = cached.get(index_clean)
    if not entry:
        await send_confirmation_text(phone_number, "Numéro invalide, veuillez réessayer.")
        return

    await _send_photos_for_product(phone_number, entry)

    # Rupture UX constatée en usage réel : consulter une photo ne doit jamais
    # faire perdre le fil d'une commande en cours — ce message n'avance PAS
    # la conversation (aucun état LangGraph touché, `photos <numéro>` est
    # entièrement hors-graphe, voir docstring de module), donc le numéro
    # affiché dans le menu d'origine (ex: choix producteur) reste valable ;
    # on le rappelle explicitement pour que l'acheteur sache qu'il peut
    # directement continuer.
    await send_confirmation_text(
        phone_number,
        f"👆 Vous pouvez répondre *{index_clean}* pour continuer avec ce producteur.",
    )


@celery_app.task(bind=True, max_retries=2, autoretry_for=(Exception,), retry_backoff=5)
def send_search_result_photos_task(
    self,
    phone_number: str = "",
    index_text: str = "",
) -> None:
    try:
        run_async(_send_search_result_photos(phone_number, index_text))
    except Exception:
        logger.exception("SEARCH_RESULT_PHOTO_FAILED | phone=%s", _mask(phone_number))
        raise
