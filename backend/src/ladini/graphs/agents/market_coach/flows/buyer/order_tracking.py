"""Order & Auction Tracking — suivi conversationnel pour acheteurs.

Fonctionnalités :
  1. list_orders       — Dashboard commandes avec menu interactif.
  2. check_order_status — Détail conversationnel d'une commande.
  3. cancel_order      — Annulation conversationnelle directe.
  4. list_buyer_auctions — Dashboard enchères (toutes statuts).
  5. check_auction_status — Détail d'une enchère + offres reçues.
  6. proactive_order_check — Greeting proactif après inactivité.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ladini.core.formatting import fmt_num as _fmt_num
from ladini.graphs.agents.market_coach.core.goals import (
    BUYER_AUCTION_TRACKING_GOALS as AUCTION_TRACKING_GOALS,
)
from ladini.graphs.agents.market_coach.core.goals import (
    BUYER_ORDER_TRACKING_GOALS as ORDER_TRACKING_GOALS,
)

# Aliases de compat — source canonique : core/goals.py (dérivés d'INTENT_CONFIG).
# NOTE : MARKET_MY_REQUESTS ∈ AUCTION_TRACKING_GOALS depuis la fusion du
# 2026-07-21 (resolve_own_auctions absorbé par list_buyer_auctions, strict
# superset : tous statuts + compte d'offres + sélection → check_auction_status
# /finalize_winner). Voir [[market-coach-turn-boundary-state]].
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    set_pending_interaction,
)

# Gate GPS partagé avec preorder.py — voir
# [[precommande-architecture-consolidation-2026-08]]. Ré-exporté sous ces
# mêmes noms pour ne pas casser les imports existants
# (`from .order_tracking import _get_stored_location`, utilisés par
# negotiation.py et preorder.py).
from ladini.graphs.agents.market_coach.flows.buyer.gps_delivery_gate import (
    _get_stored_location,  # noqa: F401 -- re-export, see comment above
    enter_gps_stage,
    resolve_gps_stage,
)
from ladini.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)
from ladini.graphs.agents.market_coach.flows.common.menu_text import (
    render_quick_actions,
    render_selection_prompt,
)
from ladini.graphs.agents.market_coach.services.mcp.error_translation import (
    BUSINESS_ERROR_CODE,
)
from ladini.graphs.agents.market_coach.services.mcp.gateway import (
    AuctionGateway,
    MCPCallError,
    OrderTrackingGateway,
)
from ladini.graphs.agents.market_coach.utils import (
    MarketRuntime,
    is_success_response,
)
from ladini.services.search_results_cache import (
    store_results as _store_search_photo_results,
)

logger = logging.getLogger("Ladini.Market.BuyerFlow.OrderTracking")


def _cache_photo_menu(
    phone: str,
    items: List[Dict[str, Any]],
    id_key: str,
    label_fn: Callable[[Dict[str, Any]], str],
) -> bool:
    """Met en cache un menu numéroté (enchères/offres) pour "photos <numéro>"
    — voir services/search_results_cache.py et
    nodes/rendering/success.py::_cache_numbered_items_with_photos (même
    principe, dupliqué ici car ces flows construisent leur `final_response`
    directement et ne passent jamais par le rendu générique). Renvoie True
    si au moins un élément a une photo."""
    if not phone or not items:
        return False
    entries: Dict[str, Dict[str, Any]] = {}
    has_photos = False
    for idx, item in enumerate(items, start=1):
        images = item.get("images") or []
        if images:
            has_photos = True
        entries[str(idx)] = {
            "id": item.get(id_key),
            "name": label_fn(item),
            "images": images,
        }
    if entries:
        _store_search_photo_results(phone, entries)
    return has_photos


# =====================================================================
# CONSTANTS
# =====================================================================

MENU_SESSION_TTL_SECONDS = 30 * 60

ORDER_STATUS_MAP = {
    "PENDING": ("⏳", "En attente de validation"),
    "DRAFT": ("📝", "Brouillon (non confirmée)"),
    "CONFIRMED": ("✅", "Confirmée — préparation en cours"),
    "PAID": ("💰", "Payée — en attente d'expédition"),
    "PROCESSING": ("🔄", "En cours de préparation"),
    "SHIPPED": ("🚛", "Expédiée — en route vers vous"),
    "IN_TRANSIT": ("🚛", "En transit"),
    "PICKED_UP": ("📍", "Arrivée au point de collecte"),
    "DELIVERED": ("📦", "Livrée avec succès"),
    "CANCELLED": ("❌", "Annulée"),
}

AUCTION_STATUS_MAP = {
    "OPEN": ("🟢", "Ouverte — en attente d'offres"),
    "PENDING": ("⏳", "En attente de validation"),
    "ACTIVE": ("🟢", "Active — offres en cours"),
    "CLOSED": ("🔒", "Clôturée"),
    "WON": ("🏆", "Remportée — offre acceptée"),
    "EXPIRED": ("⌛", "Expirée — délai dépassé"),
    "CANCELLED": ("❌", "Annulée"),
    "COMPLETED": ("✅", "Terminée"),
}

# =====================================================================
# HELPERS
# =====================================================================

_ORDER_ID_PATTERN = re.compile(
    r"(?:#|commande\s*#?|order\s*#?)\s*([A-Fa-f0-9]{6,36})",
    re.IGNORECASE,
)


def extract_order_ref(text: str) -> Optional[str]:
    if not text:
        return None
    m = _ORDER_ID_PATTERN.search(text)
    if m:
        return m.group(1).strip()
    return None


def _format_elapsed(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        minutes = max(1, int(delta.total_seconds() // 60))
        return f"il y a {minutes} min"
    if hours < 24:
        return f"il y a {hours}h"
    days = hours // 24
    return f"il y a {days} jour{'s' if days > 1 else ''}"


def _status_label(status: str, status_map: Dict[str, tuple]) -> str:
    entry = status_map.get(status.upper(), ("🔄", status))
    return f"{entry[0]} {entry[1]}"


def _resolve_order_id(state: Dict[str, Any]) -> Optional[str]:
    """Resolve order_id from payload, text, available_mapping, or tracking context."""
    payload = state.get("transaction_payload") or {}
    normalized_text = str(state.get("normalized_text") or "")

    order_id = (
        payload.get("order_id")
        or payload.get("selected_value")
        or extract_order_ref(normalized_text)
    )

    if not order_id:
        selection_idx = payload.get("selection_index")
        if selection_idx is not None:
            mapping = state.get("available_mapping") or {}
            order_id = mapping.get(str(selection_idx))

    if not order_id:
        tracking_ctx = state.get("order_tracking_context") or {}
        order_id = tracking_ctx.get("order_focus")

    return order_id


def _tracking_ctx_patch(order_id: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {"last_interaction_ts": time.time()}
    ctx["order_focus"] = order_id
    ctx.update(extra)
    return ctx


async def _safe_gw_call(
    mc_runtime: MarketRuntime, method: str, **kwargs: Any
) -> Dict[str, Any]:
    try:
        gw = OrderTrackingGateway(mc_runtime)
        fn = getattr(gw, method, None)
        if fn:
            return await fn(**kwargs)
        return await gw._call(method, **kwargs)
    except Exception as exc:
        logger.exception("order_tracking | %s | error: %s", method, exc)
        return {"status": "error", "message": "Service temporairement indisponible."}


# =====================================================================
# 1. LIST ORDERS — Dashboard avec menu interactif
# =====================================================================

_SALE_STATUS_ICONS = {
    "PENDING": "🟡",
    "CONFIRMED": "🟢",
    "COMPLETED": "✅",
    "DELIVERED": "✅",
    "IN_PROGRESS": "🔵",
    "CANCELLED": "❌",
    "FAILED": "❌",
}


async def _producer_sales_block(phone: str, mc_runtime: MarketRuntime) -> str:
    """Résumé lecture-seule des VENTES du même numéro (mêmes personnes
    peuvent acheter ET vendre sur Ladini) — signalé par un utilisateur réel :
    "mes commandes" ne montrait QUE le mode acheteur, jamais les commandes
    reçues sur ses propres produits qu'il doit préparer/livrer.

    Rendu en PUCES, JAMAIS numéroté comme la liste acheteur ci-dessus : la
    sélection numérique de ce tour (`available_mapping`) reste réservée aux
    ACHATS — numéroter aussi les ventes créerait une ambiguïté réelle
    ("2" viserait quel mapping, un achat ou une vente ?). `SALES_LIST_ORDERS`
    (menu de désambiguïsation "commandes reçues", voir interpreter/intent.py)
    reste le SEUL chemin pour agir sur une vente précise, avec sa propre
    numérotation dédiée.

    Retourne une chaîne vide (jamais None) si l'utilisateur n'a pas de profil
    producteur ou aucune vente — `list_orders` peut alors concaténer le
    résultat sans condition."""
    result = await _safe_gw_call(mc_runtime, "get_producer_orders", phone=phone)
    if not is_success_response(result):
        return ""
    sales = result.get("data") or []
    if not sales:
        return ""

    shown, extra = sales[:5], len(sales) - 5
    lines = ["\n📦 *Vos ventes à préparer/livrer :*"]
    for sale in shown:
        ref = sale.get("reference") or str(sale.get("order_id") or "")[:8].upper()
        status = str(sale.get("status") or "PENDING").upper()
        icon = _SALE_STATUS_ICONS.get(status, "🧾")
        items_summary = ", ".join(
            f"{it.get('product_name')} ({_fmt_num(it.get('quantity'))} "
            f"{str(it.get('unit') or '').lower()})"
            for it in (sale.get("items") or [])
            if it.get("product_name")
        ) or "—"
        buyer = sale.get("buyer_name") or "Acheteur"
        amount = _fmt_num(sale.get("total_amount"))
        currency = sale.get("currency") or "XOF"
        # (2026-09-11) Localisation de livraison — voir `get_producer_orders`
        # (services/database/producer.py), qui expose maintenant `maps_link`
        # (lien Google Maps si GPS connu) ou `city`/`delivery_desc` en repli.
        # Signalé manquant par un producteur réel : sans ça, "où livrer ?"
        # forçait à ouvrir l'app séparément.
        location = (
            sale.get("maps_link")
            or ", ".join(filter(None, [sale.get("city"), sale.get("delivery_desc")]))
            or None
        )
        location_line = f"\n   📍 {location}" if location else ""
        lines.append(
            f"\n• {icon} #{ref} — {items_summary}\n"
            f"   👤 {buyer} · {amount} {currency}"
            f"{location_line}"
        )
    if extra > 0:
        lines.append(f"\n… et {extra} autre{'s' if extra > 1 else ''}.")
    lines.append(
        "\n_Tapez *commandes reçues* pour voir le détail d'une vente et la "
        "gérer (numérotation distincte de la liste des achats ci-dessus)._"
    )
    return "\n".join(lines)


async def list_orders(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    phone = str(state.get("user_phone") or "")
    if not phone:
        return _error("Numéro de téléphone introuvable.")

    payload = state.get("transaction_payload") or {}
    selection_idx = payload.get("selection_index")
    if selection_idx is not None:
        mapping = state.get("available_mapping") or {}
        resolved = mapping.get(str(selection_idx))
        if resolved:
            synthetic = dict(state)
            syn_payload = dict(payload)
            syn_payload["order_id"] = resolved
            synthetic["transaction_payload"] = syn_payload
            return await check_order_status(synthetic, mc_runtime)

    result = await _safe_gw_call(mc_runtime, "get_buyer_orders_dashboard", phone=phone)
    sales_block = await _producer_sales_block(phone, mc_runtime)

    if not is_success_response(result):
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": (
                "🛒 *Vos achats*\n\n"
                "Vous n'avez pas encore passé de commande sur Ladini."
                + sales_block
                + render_quick_actions(["chercher un produit", "mes appels d'offres"])
            ),
            "ag_ui_component": None,
        }

    # (2026-09-11) `menu_text` DOIT être identique entre `final_response` et
    # `pending_menu.preformatted_text` — voir `nodes/ui_engine.py::
    # _merge_menu_text` : ce nœud ne déduplique QUE si les deux textes sont
    # strictement équivalents une fois normalisés, sinon il les concatène
    # (conçu pour le cas où le flow fournit un `final_response` COURT et
    # laisse le menu détaillé dans `preformatted_text`). Ici les deux
    # portaient un texte DIFFÉRENT (le footer de sélection n'était ajouté
    # qu'à `final_response`), donc `_merge_menu_text` affichait tout le
    # tableau de commandes DEUX FOIS — bug réel signalé par un utilisateur.
    # Le footer fait maintenant partie de `menu_text` avant tout usage, à
    # l'identique du pattern déjà correct dans `list_buyer_auctions` plus
    # bas dans ce fichier. `sales_block` (ventes, calculé plus haut) est
    # inclus ICI, dans `menu_text` lui-même — PAS ajouté séparément à
    # `final_response` seul — pour la même raison : garder les deux textes
    # strictement identiques.
    menu_text = (
        "🛒 *Vos achats (à recevoir) :*\n"
        + (result.get("formatted_menu") or "Vos commandes.")
        + render_selection_prompt(noun="commande")
        + sales_block
    )
    raw_mapping = result.get("mapping") or {}
    mapping = {
        str(i): str(oid)
        for i, oid in enumerate(raw_mapping.values(), start=1)
        if oid not in (None, "")
    }

    options = [
        MenuOption(
            index=str(idx),
            label=f"Commande #{str(oid)[:8].upper()}",
            value=oid,
        )
        for idx, oid in mapping.items()
    ]

    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.SELECTION_MENU),
        "response_strategy": "SELECTION_MENU",
        "final_response": menu_text,
        "available_mapping": mapping,
        "expected_candidates": [
            f"Commande #{oid[:8].upper()}" for oid in mapping.values()
        ],
        "order_tracking_context": _tracking_ctx_patch(menu_generated_at=time.time()),
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Vos commandes",
            options=options,
            kind="order_list",
            preformatted_text=menu_text,
        ),
    }


# =====================================================================
# 2. CHECK ORDER STATUS — Détail conversationnel
# =====================================================================


def _build_status_response(order_data: Dict[str, Any]) -> str:
    status = str(order_data.get("status") or "PENDING").upper()
    order_number = (
        order_data.get("order_number") or str(order_data.get("id", ""))[:8].upper()
    )
    total = order_data.get("total_amount") or order_data.get("total") or 0
    currency = order_data.get("currency") or "FCFA"
    created_at = order_data.get("created_at") or order_data.get("date")

    items = order_data.get("items") or []
    items_summary = ""
    if items and isinstance(items[0], dict):
        parts = []
        for it in items[:3]:
            name = it.get("name") or it.get("product_name") or "Article"
            qty = it.get("qty") or it.get("quantity") or 1
            parts.append(f"{name} (x{qty})")
        items_summary = ", ".join(parts)
        if len(items) > 3:
            items_summary += f" +{len(items) - 3} autres"

    header = f"📋 *Commande #{order_number}*"
    if items_summary:
        header += f"\n🛒 {items_summary}"
    header += f"\n💵 Total : *{total} {currency}*"

    responses = {
        "PENDING": (
            f"{header}\n\n"
            f"⏳ *Statut : En attente de validation*\n\n"
            f"La préparation n'a pas encore commencé. Le producteur va "
            f"bientôt prendre en charge votre commande.\n\n"
            f"💡 _Souhaitez-vous modifier ou annuler cette commande ?_\n"
            f"Répondez *annuler* ou *modifier*."
        ),
        "CONFIRMED": (
            f"{header}\n\n"
            f"✅ *Statut : Confirmée*\n\n"
            f"Votre commande est en cours de préparation chez le producteur. "
            f"Vous serez notifié dès l'expédition.\n\n"
            f"⏱️ _Estimation : expédition sous 24-48h._"
        ),
        "DELIVERED": (
            f"{header}\n\n"
            f"📦 *Statut : Livrée* "
            f"{_format_elapsed(datetime.fromisoformat(created_at) if isinstance(created_at, str) else created_at) if created_at else ''}\n\n"
            f"Votre commande a été livrée avec succès ! "
            f"Nous espérons que tout est conforme.\n\n"
            f"⭐ _Comment était la qualité des produits ?_\n"
            f"Répondez avec une note de 1 à 5 ou un commentaire."
        ),
        "CANCELLED": (
            f"{header}\n\n"
            f"❌ *Statut : Annulée*\n\n"
            f"Cette commande a été annulée. Les stocks ont été restitués.\n\n"
            f"🔁 _Souhaitez-vous passer une nouvelle commande ?_"
        ),
        "PICKED_UP": (
            f"{header}\n\n"
            f"📍 *Statut : Arrivée au point de collecte*\n\n"
            f"Votre commande vous attend au point de retrait. "
            f"Présentez-vous avec votre numéro de commande.\n\n"
            f"🆔 Référence : *#{order_number}*"
        ),
    }

    if status in responses:
        return responses[status]

    if status in ("SHIPPED", "IN_TRANSIT"):
        return (
            f"{header}\n\n"
            f"🚛 *Statut : En route vers vous*\n\n"
            f"Votre commande est en cours de livraison !\n\n"
            f"📍 _Restez joignable pour la réception._"
        )

    if status in ("PAID", "PROCESSING"):
        return (
            f"{header}\n\n"
            f"{_status_label(status, ORDER_STATUS_MAP)}\n\n"
            f"Votre paiement est confirmé. La commande est en préparation.\n\n"
            f"📦 _Vous recevrez une notification à l'expédition._"
        )

    return (
        f"{header}\n\n"
        f"{_status_label(status, ORDER_STATUS_MAP)}\n\n"
        f"_Tapez *aide* pour plus d'options._"
    )


async def check_order_status(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    phone = str(state.get("user_phone") or "")
    order_id = _resolve_order_id(state)

    if not order_id and not phone:
        return _error(
            "Je n'ai pas pu identifier votre commande. Quel est votre numéro de commande ?"
        )

    # buyer_phone toujours transmis (même quand order_id est connu) : requis
    # pour l'identité de contexte MCP, sinon PermissionDenied silencieux.
    kwargs: Dict[str, Any] = {}
    if order_id:
        kwargs["order_id"] = str(order_id)
    if phone:
        kwargs["buyer_phone"] = phone

    result = await _safe_gw_call(mc_runtime, "get_transaction_summary", **kwargs)

    if not is_success_response(result):
        return _order_not_found_response(order_id, phone)

    # `get_transaction_summary` renvoie status="success" + data=None quand
    # AUCUNE commande n'existe (voir services/database/buyer.py) — un
    # "succès sans résultat", pas une erreur. Sans ce garde-fou, `data`
    # retombait sur l'enveloppe elle-même (`result`) et le rendu affichait
    # l'enveloppe comme si c'était une commande : "Commande #" vide,
    # "Total : 0 FCFA", et le statut HTTP "success" affiché tel quel comme
    # statut de commande ("🔄 SUCCESS").
    if result.get("data") is None:
        return _order_not_found_response(order_id, phone)

    data = result.get("data") or result
    if isinstance(data, dict) and "status" not in data and "order_id" not in data:
        data = result

    resolved_order_id = data.get("order_id") or data.get("id") or order_id

    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": _build_status_response(data),
        "order_tracking_context": _tracking_ctx_patch(
            resolved_order_id, last_status=data.get("status")
        ),
        "ag_ui_component": None,
    }


# =====================================================================
# 3. CANCEL ORDER
# =====================================================================


async def cancel_order(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = dict(state.get("transaction_payload") or {})
    phone = str(state.get("user_phone") or "")
    order_id = _resolve_order_id(state)

    if not order_id:
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.ENTER_FIELD, field_name="order_id"),
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": (
                "Quelle commande souhaitez-vous annuler ?\n\n"
                "💡 _Indiquez le numéro (ex: #ABC12345) ou tapez *mes commandes* "
                "pour voir la liste._"
            ),
            "ag_ui_component": None,
        }

    payload["order_id"] = str(order_id)

    cancel_reason = payload.get("cancel_reason") or payload.get("reason")
    if cancel_reason in (None, "", [], {}):
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.ENTER_FIELD, field_name="cancellation_reason"),
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": (
                "Pour annuler cette commande, merci d'indiquer la raison.\n"
                "💡 Expliquez en quelques mots (ex: délai trop long, erreur de produit)."
            ),
            "transaction_payload": payload,
            "order_tracking_context": _tracking_ctx_patch(order_id),
            "ag_ui_component": None,
        }

    gw = OrderTrackingGateway(mc_runtime)
    try:
        result = await gw.cancel_pending_order(
            order_id=str(order_id),
            phone=phone,
            reason=str(cancel_reason).strip(),
        )
    except Exception as exc:
        logger.exception("cancel_order | error: %s", exc)
        result = {"status": "error", "message": "Service temporairement indisponible."}

    status = str(result.get("status") or "").lower()
    msg = result.get("message") or ""

    if status == "success":
        cleaned_payload = {**payload}
        cleaned_payload.pop("cancel_reason", None)
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": (
                f"{msg}\n\n"
                f"🔁 _Souhaitez-vous passer une nouvelle commande ou "
                f"voir vos commandes actives ?_"
            ),
            "order_tracking_context": _tracking_ctx_patch(None),
            "transaction_payload": cleaned_payload,
            "ag_ui_component": None,
        }

    if "statut" in msg.lower() or "impossible" in msg.lower():
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": (
                f"⚠️ {msg}\n\n"
                # (2026-09-04, audit produit post-F1-F4) : `cancel_pending_order`
                # accepte désormais aussi les commandes CONFIRMÉES (pas
                # seulement PENDING) — copie alignée sur le garde réel.
                f"Seules les commandes *en attente* ou *confirmées, pas encore "
                f"livrées* peuvent être annulées.\n"
                f"_Souhaitez-vous voir le statut actuel de cette commande ?_"
            ),
            "order_tracking_context": _tracking_ctx_patch(order_id),
            "ag_ui_component": None,
        }

    return _order_not_found_response(order_id, phone)


# =====================================================================
# 4. LIST BUYER AUCTIONS — Dashboard enchères (tous statuts)
# =====================================================================


async def list_buyer_auctions(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    """Liste toutes les enchères de l'acheteur avec leur statut."""
    phone = str(state.get("user_phone") or "")
    if not phone:
        return _error("Numéro de téléphone introuvable.")

    payload = state.get("transaction_payload") or {}
    selection_idx = payload.get("selection_index")
    if selection_idx is not None:
        mapping = state.get("available_mapping") or {}
        resolved = mapping.get(str(selection_idx))
        if resolved:
            synthetic = dict(state)
            syn_payload = dict(payload)
            syn_payload["auction_id"] = resolved
            synthetic["transaction_payload"] = syn_payload
            return await check_auction_status(synthetic, mc_runtime)

    gw = AuctionGateway(mc_runtime)
    result = await gw.search_auctions(phone=phone, view_mode="MY_OWN")

    if not is_success_response(result):
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": (
                "📋 *Vos appels d'offres*\n\n"
                "Vous n'avez aucun appel d'offres pour le moment."
                + render_quick_actions(
                    ["lancer un appel d'offres", "chercher un produit"]
                )
            ),
            "ag_ui_component": None,
        }

    data = result.get("data") or []
    if not data:
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": (
                "📋 *Vos appels d'offres*\n\n"
                "Vous n'avez aucun appel d'offres pour le moment."
                + render_quick_actions(
                    ["lancer un appel d'offres", "chercher un produit"]
                )
            ),
            "ag_ui_component": None,
        }

    lines = ["📋 *Vos Appels d'Offres :*\n"]
    mapping: Dict[str, str] = {}
    options: List[MenuOption] = []

    for i, auction in enumerate(data, start=1):
        auction_id = str(auction.get("auction_id") or auction.get("id") or "")
        product = auction.get("product") or auction.get("product_name") or "Produit"
        status_raw = str(auction.get("status") or "OPEN").upper()
        status_label = _status_label(status_raw, AUCTION_STATUS_MAP)
        qty = auction.get("quantity") or ""
        unit = auction.get("unit") or ""
        bid_count = auction.get("bid_count") or auction.get("offers_count") or 0
        price = auction.get("max_price") or auction.get("budget") or ""

        qty_str = f" — {qty} {unit}" if qty else ""
        price_str = f" — Budget: {price} FCFA" if price else ""
        bids_str = (
            f" — 📥 {bid_count} proposition{'s' if int(bid_count) > 1 else ''}"
            if bid_count
            else ""
        )

        label = f"{product}{qty_str}{price_str}"
        lines.append(f"*{i}.* {label}\n   {status_label}{bids_str}")
        mapping[str(i)] = auction_id
        options.append(MenuOption(index=str(i), label=label, value=auction_id))

    # Deux sources de photo bien distinctes, jamais mélangées : la photo de
    # RÉFÉRENCE de l'enchère elle-même (add_auction_photo, optionnelle) VS
    # une photo jointe par un PRODUCTEUR à SON offre (add_bid_photo). "photos
    # <numéro>" à ce niveau liste ne connaît QUE la première (c'est ce qui
    # est mis en cache ci-dessous) — sans ce distinguo, une enchère sans
    # photo de référence mais dont l'unique offre reçue en a une paraissait
    # "sans photo" alors qu'il y en a bien une à voir en sélectionnant le
    # numéro (check_auction_status, déjà câblé). Bug réel signalé le
    # 2026-08-13 : le hint n'apparaissait jamais dans ce cas précis.
    has_reference_photos = _cache_photo_menu(
        phone,
        data,
        "auction_id",
        lambda a: a.get("product") or a.get("product_name") or "Appel d'offres",
    )
    if has_reference_photos:
        lines.append(
            "\n📸 Tapez *photos <numéro>* pour voir la photo de référence d'un appel d'offres."
        )
    elif any(a.get("has_bid_photos") for a in data):
        lines.append(
            "\n📸 Une ou plusieurs offres reçues ont des photos — "
            "sélectionnez le numéro de l'appel d'offres pour les voir."
        )

    lines.append(render_selection_prompt(noun="appel d'offres"))
    menu_text = "\n".join(lines)

    wm = dict(state.get("working_memory") or {})
    wm["available_mapping_kind"] = "buyer_auction_list"
    wm["winner_auction_id"] = None
    wm["pending_winner_bid"] = None

    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.SELECTION_MENU),
        "response_strategy": "SELECTION_MENU",
        "final_response": menu_text,
        "available_mapping": mapping,
        "expected_candidates": [
            f"Appel d'offres #{oid[:8]}" for oid in mapping.values() if oid
        ],
        "working_memory": wm,
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Vos appels d'offres",
            options=options,
            kind="buyer_auction_list",
            preformatted_text=menu_text,
        ),
    }


# =====================================================================
# 5. CHECK AUCTION STATUS — Détail + offres reçues
# =====================================================================


async def check_auction_status(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    """Affiche le détail d'un appel d'offres + les propositions reçues dessus."""
    payload = state.get("transaction_payload") or {}

    auction_id = payload.get("auction_id") or payload.get("selected_value")
    if not auction_id:
        selection_idx = payload.get("selection_index")
        if selection_idx is not None:
            mapping = state.get("available_mapping") or {}
            auction_id = mapping.get(str(selection_idx))

    if not auction_id:
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.SELECTION_MENU),
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": (
                "Quel appel d'offres souhaitez-vous consulter ?\n"
                "💡 _Tapez *mes appels d'offres* pour voir la liste._"
            ),
            "ag_ui_component": None,
        }

    gw = AuctionGateway(mc_runtime)
    bids_result = await gw.get_auction_bids(
        auction_id=str(auction_id), phone=str(state.get("user_phone") or "")
    )

    bids = bids_result.get("bids") or bids_result.get("data") or []
    auction_info = bids_result.get("auction") or {}
    product = (
        auction_info.get("product")
        or auction_info.get("product_name")
        or "Votre produit"
    )
    status_raw = str(
        auction_info.get("status") or bids_result.get("auction_status") or "OPEN"
    ).upper()
    status_label = _status_label(status_raw, AUCTION_STATUS_MAP)

    lines = [
        f"📋 *Appel d'offres — {product}*",
        f"Statut : {status_label}",
    ]

    if not bids:
        lines.append("\n📭 _Aucune proposition reçue pour le moment._")
        if status_raw == "OPEN":
            lines.append("Les producteurs peuvent encore soumettre des propositions.")
    else:
        lines.append(
            f"\n📥 *{len(bids)} proposition{'s' if len(bids) > 1 else ''} reçue{'s' if len(bids) > 1 else ''} :*"
        )
        mapping: Dict[str, str] = {}
        options: List[MenuOption] = []

        for i, bid in enumerate(bids, start=1):
            bid_id = str(bid.get("bid_id") or bid.get("id") or "")
            producer = bid.get("producer") or bid.get("producer_name") or "Producteur"
            price = bid.get("price") or bid.get("offered_price") or "?"
            bid_status = str(bid.get("status") or "PENDING").upper()
            bid_emoji = (
                "🟡"
                if bid_status == "PENDING"
                else "✅"
                if bid_status == "ACCEPTED"
                else "🔴"
            )

            label = f"{producer} — {price} FCFA"
            lines.append(f"\n*{i}.* {bid_emoji} {label}")
            mapping[str(i)] = bid_id
            options.append(MenuOption(index=str(i), label=label, value=bid_id))

        # Photo du lot proposé (add_bid_photo côté producteur) — le moment de
        # confiance clé avant de désigner un gagnant. Voir
        # [[auction-bid-photos-2026-08]].
        if _cache_photo_menu(
            str(state.get("user_phone") or ""),
            bids,
            "bid_id",
            lambda b: (
                f"{b.get('producer') or b.get('producer_name') or 'Producteur'} — {product}"
            ),
        ):
            lines.append(
                "\n📸 Tapez *photos <numéro>* pour voir la photo d'un lot proposé."
            )

        if status_raw == "OPEN":
            lines.append(
                "\n_Répondez avec le *numéro* de la proposition pour désigner le gagnant._"
            )

            # kind="bid" → memory_update résout la sélection en payload.bid_id
            # (et NON auction_id : "auction_bids" est mappé vers auction_id, ce
            # qui écraserait l'id de l'enchère). On garde l'enchère de contexte
            # dans working_memory.winner_auction_id.
            wm = dict(state.get("working_memory") or {})
            wm["winner_auction_id"] = str(auction_id)
            payload_out = dict(state.get("transaction_payload") or {})
            payload_out["auction_id"] = str(auction_id)
            payload_out["selection_index"] = None
            payload_out["bid_id"] = None

            return {
                "status": "WAITING_INPUT",
                **set_pending_interaction(InteractionKind.SELECTION_MENU),
                "response_strategy": "SELECTION_MENU",
                "final_response": "\n".join(lines),
                "available_mapping": mapping,
                "current_goal": "BUYER_CHECK_AUCTION_STATUS",
                "transaction_payload": payload_out,
                "working_memory": wm,
                "ag_ui_component": None,
                "pending_menu": MenuRequest(
                    title=f"Propositions — {product}",
                    options=options,
                    kind="bid",
                    metadata={"auction_id": str(auction_id)},
                    preformatted_text="\n".join(lines),
                ),
            }

    lines.append("")
    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": "\n".join(lines),
        "ag_ui_component": None,
    }


# =====================================================================
# 5b. WINNER SELECTION — désigner le gagnant d'une enchère
# =====================================================================

_YES_TOKENS = frozenset(
    {
        "oui",
        "ok",
        "okay",
        "daccord",
        "d'accord",
        "c'est bon",
        "cest bon",
        "confirme",
        "confirmer",
        "je confirme",
        "valide",
        "valider",
        "go",
        "vasy",
        "parfait",
        "yes",
    }
)
_NO_TOKENS = frozenset(
    {
        "non",
        "annuler",
        "annule",
        "stop",
        "cancel",
        "quitter",
        "pas maintenant",
        "retour",
    }
)


def _selection_index(state: Dict[str, Any]) -> Optional[int]:
    payload = state.get("transaction_payload") or {}
    raw = payload.get("selection_index")
    if raw is None:
        raw = (state.get("extracted_entities") or {}).get("selection_index")
    if raw is None:
        txt = str(state.get("normalized_text") or "").strip()
        if txt.isdigit():
            raw = txt
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


async def _fetch_winner_recap(
    mc_runtime: MarketRuntime, auction_id: Optional[str], bid_id: str, phone: str
) -> Dict[str, Any]:
    """Projette le récap "vous allez retenir X" depuis l'état RÉEL de
    l'offre, jamais depuis un texte mis en cache (2026-09-04, correctif
    stale recap). SEULE fonction qui sait construire ce récap — utilisée à
    la fois pour l'affichage initial (`confirm_winner_selection`) et pour
    la revalidation juste avant exécution (`_execute_winner_selection`) :
    un seul point de vérité, jamais deux implémentations qui pourraient
    diverger."""
    producer = "ce producteur"
    price = None
    product = "votre produit"
    bid_status: Optional[str] = None
    if auction_id:
        gw = AuctionGateway(mc_runtime)
        try:
            detail = await gw.get_auction_bids(auction_id=str(auction_id), phone=phone)
            product = (detail.get("auction") or {}).get("product") or product
            for b in detail.get("bids") or []:
                if str(b.get("bid_id") or b.get("id")) == str(bid_id):
                    producer = b.get("producer") or b.get("producer_name") or producer
                    price = b.get("price") or b.get("offered_price")
                    bid_status = str(b.get("status") or "").upper() or None
                    break
        except Exception as exc:
            logger.warning("_fetch_winner_recap: refetch failed: %s", exc)
    return {
        "producer": producer,
        "price": price,
        "product": product,
        "bid_status": bid_status,
    }


async def confirm_winner_selection(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    """L'acheteur a choisi une offre : afficher un récap + demander confirmation."""
    working = state.get("working_memory") or {}
    payload = state.get("transaction_payload") or {}
    auction_id = working.get("winner_auction_id")
    mapping = state.get("available_mapping") or {}
    sel = _selection_index(state)

    # memory_update (kind="bid") a normalement déjà posé payload.bid_id ; sinon
    # on retombe sur le mapping index→bid_id.
    bid_id = payload.get("bid_id") or (
        mapping.get(str(sel)) if sel is not None else None
    )
    if not bid_id:
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.SELECTION_MENU),
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": "Indiquez le *numéro* de la proposition à retenir (ex: 1).",
            "ag_ui_component": None,
        }

    recap = await _fetch_winner_recap(
        mc_runtime, auction_id, str(bid_id), str(state.get("user_phone") or "")
    )
    price_txt = (
        f" à *{_fmt_num(recap['price'])} FCFA*" if recap["price"] is not None else ""
    )

    wm = dict(working)
    wm["available_mapping_kind"] = "confirm_winner"
    wm["pending_winner_bid"] = str(bid_id)
    # (2026-09-04, correctif stale recap) : le prix EFFECTIVEMENT montré à
    # l'acheteur devient la cible de confirmation — pas juste un texte,
    # une VALEUR comparable. `_execute_winner_selection` la revalide contre
    # l'état réel juste avant d'exécuter (voir sa docstring) : c'est CETTE
    # comparaison, pas un `refresh_confirmation()` cosmétique, qui ferme
    # la fenêtre de péremption autour de l'étape GPS.
    wm["pending_winner_price"] = recap["price"]

    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation"),
        "response_strategy": "ASK_MISSING_FIELD",
        "current_goal": "BUYER_CHECK_AUCTION_STATUS",
        "final_response": (
            f"🤝 Vous allez retenir la proposition de *{recap['producer']}*{price_txt} "
            f"pour *{recap['product']}*.\n\n"
            "⚠️ Cette action *clôture l'appel d'offres* et crée la commande.\n"
            "👉 Répondez *oui* pour confirmer, ou *non* pour annuler."
        ),
        "working_memory": wm,
        "ag_ui_component": None,
    }


async def _execute_winner_selection(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
    bid_id: str,
    clear_wm: "Callable[[], Dict[str, Any]]",
    delivery_lat: Optional[float] = None,
    delivery_lon: Optional[float] = None,
    auction_id: Optional[str] = None,
    expected_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Exécute la sélection — MAIS revalide d'abord contre l'état RÉEL
    (2026-09-04, correctif stale recap).

    Entre le "oui" de l'acheteur (`confirm_winner_selection`, où
    `expected_price` a été capturé) et cet appel, une étape GPS s'intercale
    (1+ tour conversationnel). Le producteur reste libre de modifier son
    prix ou de retirer son offre PENDANT cette fenêtre (`Bid` n'est
    verrouillé/figé qu'au moment de `select_winning_bid` lui-même — aucune
    nouvelle table/Draft introduite pour "geler" plus tôt, voir le rapport
    d'audit). La BASE reste toujours cohérente dans tous les cas (le verrou
    de `select_winning_bid` garantit qu'elle utilise TOUJOURS le prix
    réellement courant) — ce qui manquait, c'est que le RÉCAP déjà montré à
    l'acheteur pouvait diverger silencieusement de ce qui allait réellement
    s'exécuter. On revalide donc ICI, juste avant d'exécuter : si le prix a
    changé ou que l'offre n'est plus sélectionnable, on n'exécute JAMAIS
    silencieusement sur l'ancienne valeur — on reconstruit le récap à
    partir de `_fetch_winner_recap` (LA même projection que l'affichage
    initial, jamais un second calcul divergent) et on redemande une
    confirmation EXPLICITE sur l'état actuel."""
    if auction_id and expected_price is not None:
        recap = await _fetch_winner_recap(
            mc_runtime, auction_id, str(bid_id), str(state.get("user_phone") or "")
        )
        current_status = recap["bid_status"]
        current_price = recap["price"]

        if current_status is not None and current_status != "PENDING":
            return {
                "status": "COMPLETED",
                "response_strategy": "ERROR",
                "final_response": (
                    "⚠️ Cette proposition n'est plus disponible (retirée ou déjà "
                    "traitée entre-temps). Tapez *mes appels d'offres* pour revoir "
                    "les offres actuelles."
                ),
                "working_memory": clear_wm(),
                "ag_ui_component": None,
            }

        if current_price is not None and float(current_price) != float(expected_price):
            wm = dict(state.get("working_memory") or {})
            wm["pending_winner_bid"] = str(bid_id)
            wm["pending_winner_price"] = current_price
            wm["winner_gps_stage"] = None
            wm["winner_gps_default"] = None
            return {
                "status": "WAITING_INPUT",
                **set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation"),
                "response_strategy": "ASK_MISSING_FIELD",
                "final_response": (
                    f"⚠️ Le prix de cette offre a changé entre-temps : il est "
                    f"maintenant *{_fmt_num(current_price)} FCFA*.\n\n"
                    "👉 Répondez *oui* pour confirmer à ce nouveau prix, ou *non* pour annuler."
                ),
                "working_memory": wm,
                "ag_ui_component": None,
            }

    gw = AuctionGateway(mc_runtime)
    try:
        result = await gw.select_winning_bid(
            bid_id=str(bid_id),
            phone=str(state.get("user_phone") or ""),
            delivery_lat=delivery_lat,
            delivery_lon=delivery_lon,
        )
    except MCPCallError as exc:
        # (2026-09-02, taxonomie d'erreurs) : un BUSINESS_ERROR_CODE (ex:
        # BusinessRuleException "hors du Burkina Faso" levée par
        # services/database/auction.py::select_winning_bid, geofencing en
        # défense en profondeur) porte un message déjà traduit et
        # actionnable (error_translation.py) — ne JAMAIS le remplacer par le
        # générique "réessayez", qui laisse croire qu'un nouvel essai
        # pourrait suffire alors que la cause ne changera pas. Réservé aux
        # VRAIES pannes techniques (INFRA_ERROR_CODE).
        logger.warning("finalize_winner: select_winning_bid rejeté: %s", exc)
        is_business = exc.error_code == BUSINESS_ERROR_CODE
        return {
            "status": "COMPLETED",
            "response_strategy": "ERROR",
            "final_response": (
                str(exc)
                if is_business
                else "Impossible de valider le gagnant pour le moment. Réessayez dans un instant."
            ),
            "working_memory": clear_wm(),
            "ag_ui_component": None,
        }
    except Exception as exc:
        logger.exception("finalize_winner: select_winning_bid failed: %s", exc)
        return {
            "status": "COMPLETED",
            "response_strategy": "ERROR",
            "final_response": "Impossible de valider le gagnant pour le moment. Réessayez dans un instant.",
            "working_memory": clear_wm(),
            "ag_ui_component": None,
        }

    if not is_success_response(result):
        return {
            "status": "COMPLETED",
            "response_strategy": "ERROR",
            "final_response": result.get("message")
            or "Cette proposition n'a pas pu être retenue.",
            "working_memory": clear_wm(),
            "ag_ui_component": None,
        }

    summary = (
        result.get("summary_buyer")
        or "🤝 Proposition retenue ! La commande a été créée et le producteur informé."
    )
    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": summary,
        "working_memory": clear_wm(),
        "available_mapping": {},
        "transaction_payload": {"__reset__": True},
        "ag_ui_component": None,
    }


async def finalize_winner(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    """Traite la réponse oui/non à la confirmation du gagnant, PUIS (une fois
    le gagnant confirmé) le point GPS de livraison via le gate partagé
    `gps_delivery_gate.py` (voir [[precommande-architecture-consolidation-2026-08]]
    et [[gps-delivery-burkina-faso-2026-08]]). Deux étapes distinctes
    partageant le même `expected_input == "CONFIRMATION"` et le même
    marqueur `pending_winner_bid` (voir order_tracking_resolver),
    différenciées par `working_memory.winner_gps_stage`.
    """
    working = state.get("working_memory") or {}
    bid_id = working.get("pending_winner_bid")
    event = str(state.get("interpreted_event") or "").upper().strip()
    text = str(state.get("normalized_text") or "").strip().lower()
    location_shared = bool(state.get("location_shared"))
    phone = str(state.get("user_phone") or "")

    def _clear_wm() -> Dict[str, Any]:
        wm = dict(working)
        for k in (
            "available_mapping_kind",
            "pending_winner_bid",
            "pending_winner_price",
            "winner_auction_id",
            "winner_gps_stage",
            "winner_gps_default",
        ):
            wm[k] = None
        return wm

    is_no = event == "REJECT" or text in _NO_TOKENS
    is_yes = event == "CONFIRM" or text in _YES_TOKENS

    if not bid_id or is_no:
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": (
                "D'accord, aucune proposition n'a été retenue. "
                "Tapez *mes appels d'offres* pour les revoir."
            ),
            "working_memory": _clear_wm(),
            "available_mapping": {},
            "ag_ui_component": None,
        }

    gps_stage = bool(working.get("winner_gps_stage"))

    # ── Étape 1 : confirmer le GAGNANT lui-même (comportement historique) ──
    if not gps_stage:
        if not is_yes:
            from ladini.graphs.agents.market_coach.utils import llm_deviation_reply

            note = await llm_deviation_reply(
                mc_runtime,
                str(state.get("normalized_text") or ""),
                "confirmer le gagnant retenu pour cet appel d'offres (oui/non)",
            )
            prompt = "Répondez *oui* pour confirmer le gagnant, ou *non* pour annuler."
            return {
                "status": "WAITING_INPUT",
                **set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation"),
                "response_strategy": "ASK_MISSING_FIELD",
                "final_response": f"{note}\n\n{prompt}" if note else prompt,
                "ag_ui_component": None,
            }

        # "oui" reçu pour le gagnant → étape GPS AVANT d'exécuter la commande
        # (jamais de commande sans point de livraison connu — Burkina Faso
        # uniquement, adresses textuelles trop imprécises).
        gate = await enter_gps_stage(mc_runtime, phone)
        wm = dict(working)
        wm["winner_gps_stage"] = True
        wm["winner_gps_default"] = gate["gps_default"]
        return {
            "status": "WAITING_INPUT",
            # (2026-09-02, validation réelle) : PROVIDE_LOCATION, pas
            # CONFIRM_ACTION — voir le commentaire jumeau dans preorder.py.
            **set_pending_interaction(InteractionKind.PROVIDE_LOCATION, context_ref="confirmation"),
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": gate["prompt"],
            "working_memory": wm,
            "ag_ui_component": None,
        }

    # ── Étape 2 : point GPS de livraison ────────────────────────────────
    resolution = await resolve_gps_stage(
        mc_runtime,
        phone,
        location_shared=location_shared,
        is_yes=is_yes,
        gps_default=working.get("winner_gps_default"),
        user_text=str(state.get("normalized_text") or ""),
        location_outcome=state.get("location_outcome"),
        location_lat=state.get("location_lat"),
        location_lon=state.get("location_lon"),
    )
    if resolution.resolved:
        return await _execute_winner_selection(
            state,
            mc_runtime,
            bid_id,
            _clear_wm,
            resolution.lat,
            resolution.lon,
            auction_id=working.get("winner_auction_id"),
            expected_price=working.get("pending_winner_price"),
        )
    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.PROVIDE_LOCATION, context_ref="confirmation"),
        "response_strategy": "ASK_MISSING_FIELD",
        "final_response": resolution.message,
        "ag_ui_component": None,
    }


# =====================================================================
# 6. PROACTIVE ORDER CHECK
# =====================================================================


async def proactive_order_check(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Optional[Dict[str, Any]]:
    phone = str(state.get("user_phone") or "")
    if not phone:
        return None

    tracking_ctx = state.get("order_tracking_context") or {}
    last_ts = tracking_ctx.get("last_interaction_ts") or 0
    if time.time() - last_ts < 3600:
        return None

    result = await _safe_gw_call(mc_runtime, "get_buyer_orders_dashboard", phone=phone)
    if not is_success_response(result):
        return None

    mapping = result.get("mapping") or {}
    if not mapping:
        return None

    first_order_id = next(iter(mapping.values()), None)
    if not first_order_id:
        return None

    detail = await _safe_gw_call(
        mc_runtime, "get_transaction_summary", order_id=first_order_id
    )
    if not is_success_response(detail):
        return None

    data = detail.get("data") or detail
    status = str(data.get("status") or "").upper()
    order_number = data.get("order_number") or str(first_order_id)[:8].upper()
    label = _status_label(status, ORDER_STATUS_MAP)

    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": (
            f"👋 Bonjour ! Votre commande *#{order_number}* est actuellement :\n"
            f"{label}\n\n"
            f"_Souhaitez-vous des détails ? Tapez *oui* ou *statut*._"
        ),
        "order_tracking_context": _tracking_ctx_patch(first_order_id),
        "ag_ui_component": None,
    }


# =====================================================================
# 7. ORCHESTRATOR
# =====================================================================


async def order_tracking_resolver(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    goal = str(state.get("current_goal") or "").upper().strip()
    working = state.get("working_memory") or {}
    payload = state.get("transaction_payload") or {}

    # --- Machine à états : désignation du gagnant (prioritaire) ---
    # NB : memory_update type déjà la sélection (kind="bid"→payload.bid_id,
    # kind="buyer_auction_list"→payload.auction_id) et retire selection_index.
    #
    # 1. Confirmation oui/non d'un gagnant déjà choisi.
    # Garde-fou sur interpreted_event EN PLUS du goal : `BUYER_LIST_AUCTIONS`
    # est lui-même dans AUCTION_TRACKING_GOALS, donc un `goal in
    # AUCTION_TRACKING_GOALS` seul ne suffit PAS à empêcher un
    # `pending_winner_bid` fantôme de détourner "lister mes appels d'offres"
    # vers la confirmation du gagnant (bug vu en prod : sélection du menu
    # "1. Voir mes appels d'offres" → répond "Répondez oui pour confirmer le
    # gagnant"). Un `expected_input == "CONFIRMATION"` a été essayé mais
    # `validator.py` le réinitialise INCONDITIONNELLEMENT à "NONE" dès que le
    # goal courant n'a aucun required field (le cas de tous les goals
    # `handled_by_flow` comme BUYER_CHECK_AUCTION_STATUS) — AVANT que ce
    # resolver ne s'exécute, cassant le VRAI "oui" de confirmation (boucle
    # infinie observée en prod : "oui" → validator remet expected_input=NONE
    # → resolver retombe sur check_auction_status au lieu de finalize_winner).
    # `interpreted_event`, lui, n'est JAMAIS touché par validator.py et vaut
    # exactement "CONFIRM"/"REJECT" pendant le tour où l'utilisateur répond
    # réellement oui/non (c'est d'ailleurs ce que `finalize_winner` vérifie en
    # interne). Coupler flag + event rend le détournement impossible sans
    # bloquer la vraie confirmation.
    # `or location_shared` : une fois le gagnant confirmé ("oui"), finalize_winner
    # entre dans une 2e étape (confirmer/partager le point GPS de livraison,
    # voir [[gps-delivery-burkina-faso-2026-08]]) — un partage de position
    # natif WhatsApp n'a pas de texte à classifier CONFIRM/REJECT, donc sans
    # ce OR ce tour ne route JAMAIS vers finalize_winner et la position
    # partagée est ignorée par cette machine à états (elle reste quand même
    # persistée en tâche de fond côté webhook, juste pas utilisée ICI).
    if (
        working.get("pending_winner_bid")
        and goal in AUCTION_TRACKING_GOALS
        and (
            str(state.get("interpreted_event") or "").upper() in {"CONFIRM", "REJECT"}
            or bool(state.get("location_shared"))
        )
    ):
        return await finalize_winner(state, mc_runtime)
    # 2. Une offre vient d'être choisie sur une enchère de l'acheteur → récap.
    if (
        goal in AUCTION_TRACKING_GOALS
        and payload.get("bid_id")
        and working.get("winner_auction_id")
    ):
        return await confirm_winner_selection(state, mc_runtime)
    # 3. Une enchère vient d'être choisie dans la liste → afficher ses offres.
    if (
        goal in AUCTION_TRACKING_GOALS
        and payload.get("auction_id")
        and not payload.get("bid_id")
    ):
        return await check_auction_status(state, mc_runtime)

    if goal == "BUYER_CHECK_ORDER_STATUS":
        return await check_order_status(state, mc_runtime)
    if goal == "BUYER_CANCEL_ORDER":
        return await cancel_order(state, mc_runtime)
    if goal in ("BUYER_LIST_AUCTIONS", "MARKET_MY_REQUESTS"):
        return await list_buyer_auctions(state, mc_runtime)
    if goal == "BUYER_CHECK_AUCTION_STATUS":
        return await check_auction_status(state, mc_runtime)

    return await list_orders(state, mc_runtime)


# =====================================================================
# INTERNAL HELPERS
# =====================================================================


def _error(message: str) -> Dict[str, Any]:
    return {
        "status": "ERROR",
        "response_strategy": "ERROR",
        "final_response": message,
        "ag_ui_component": None,
    }


def _order_not_found_response(order_id: Optional[str], phone: str) -> Dict[str, Any]:
    ref = f" *#{order_id[:8].upper()}*" if order_id else ""
    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": (
            f"🔍 Je n'ai pas trouvé de commande{ref} dans votre historique.\n\n"
            f"Voulez-vous voir la liste de vos commandes actives ?\n"
            f"👉 Tapez *mes commandes*"
        ),
        "order_tracking_context": _tracking_ctx_patch(None),
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Commande introuvable",
            options=[
                MenuOption(
                    index="1", label="📦 Voir mes commandes", value="BUYER_LIST_ORDERS"
                ),
                MenuOption(
                    index="2", label="🛒 Nouvelle commande", value="BUYER_ADD_TO_CART"
                ),
            ],
            kind="order_recovery",
        ),
    }


__all__ = [
    "ORDER_TRACKING_GOALS",
    "AUCTION_TRACKING_GOALS",
    "AUCTION_STATUS_MAP",
    "ORDER_STATUS_MAP",
    "extract_order_ref",
    "check_order_status",
    "list_orders",
    "cancel_order",
    "list_buyer_auctions",
    "check_auction_status",
    "proactive_order_check",
    "order_tracking_resolver",
]
