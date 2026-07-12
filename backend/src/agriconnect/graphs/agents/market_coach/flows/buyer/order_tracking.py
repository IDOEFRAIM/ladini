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
from typing import Any, Dict, List, Optional

from agriconnect.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)
from agriconnect.graphs.agents.market_coach.flows.common.menu_text import (
    render_quick_actions,
    render_selection_prompt,
)
from agriconnect.graphs.agents.market_coach.services.mcp.gateway import (
    AuctionGateway,
    OrderTrackingGateway,
)
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    is_success_response,
)

logger = logging.getLogger("AgriConnect.Market.BuyerFlow.OrderTracking")


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

ORDER_TRACKING_GOALS = frozenset({
    "BUYER_CHECK_ORDER_STATUS",
    "BUYER_LIST_ORDERS",
    "BUYER_CANCEL_ORDER",
})

AUCTION_TRACKING_GOALS = frozenset({
    "BUYER_LIST_AUCTIONS",
    "BUYER_CHECK_AUCTION_STATUS",
})


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


async def _safe_gw_call(mc_runtime: MarketRuntime, method: str, **kwargs: Any) -> Dict[str, Any]:
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

    if not is_success_response(result):
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": (
                "📦 *Vos commandes*\n\n"
                "Vous n'avez pas encore passé de commande sur AgriConnect."
                + render_quick_actions(["chercher un produit", "mes enchères"])
            ),
            "ag_ui_component": None,
        }

    menu_text = result.get("formatted_menu") or "Vos commandes."
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
        "expected_input": "SELECTION",
        "response_strategy": "SELECTION_MENU",
        "final_response": menu_text + render_selection_prompt(noun="commande"),
        "available_mapping": mapping,
        "expected_candidates": [f"Commande #{oid[:8].upper()}" for oid in mapping.values()],
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
    order_number = order_data.get("order_number") or str(order_data.get("id", ""))[:8].upper()
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
        return _error("Je n'ai pas pu identifier votre commande. Quel est votre numéro de commande ?")

    kwargs: Dict[str, Any] = {}
    if order_id:
        kwargs["order_id"] = str(order_id)
    else:
        kwargs["buyer_phone"] = phone

    result = await _safe_gw_call(mc_runtime, "get_transaction_summary", **kwargs)

    if not is_success_response(result):
        return _order_not_found_response(order_id, phone)

    data = result.get("data") or result
    if isinstance(data, dict) and "status" not in data and "order_id" not in data:
        data = result

    resolved_order_id = data.get("order_id") or data.get("id") or order_id

    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": _build_status_response(data),
        "order_tracking_context": _tracking_ctx_patch(resolved_order_id, last_status=data.get("status")),
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
            "expected_input": "ORDER_ID",
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
            "expected_input": "CANCELLATION_REASON",
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
                f"Seules les commandes *en attente* (⏳) peuvent être annulées.\n"
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
                + render_quick_actions(["lancer un appel d'offres", "chercher un produit"])
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
                + render_quick_actions(["lancer un appel d'offres", "chercher un produit"])
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
        bids_str = f" — 📥 {bid_count} offre{'s' if int(bid_count) > 1 else ''}" if bid_count else ""

        label = f"{product}{qty_str}{price_str}"
        lines.append(f"*{i}.* {label}\n   {status_label}{bids_str}")
        mapping[str(i)] = auction_id
        options.append(MenuOption(index=str(i), label=label, value=auction_id))

    lines.append(render_selection_prompt(noun="enchère"))
    menu_text = "\n".join(lines)

    wm = dict(state.get("working_memory") or {})
    wm["available_mapping_kind"] = "buyer_auction_list"
    wm.pop("winner_auction_id", None)
    wm.pop("pending_winner_bid", None)

    return {
        "status": "WAITING_INPUT",
        "expected_input": "SELECTION",
        "response_strategy": "SELECTION_MENU",
        "final_response": menu_text,
        "available_mapping": mapping,
        "expected_candidates": [f"Enchère #{oid[:8]}" for oid in mapping.values() if oid],
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
    """Affiche le détail d'une enchère + les offres reçues dessus."""
    payload = state.get("transaction_payload") or {}

    auction_id = (
        payload.get("auction_id")
        or payload.get("selected_value")
    )
    if not auction_id:
        selection_idx = payload.get("selection_index")
        if selection_idx is not None:
            mapping = state.get("available_mapping") or {}
            auction_id = mapping.get(str(selection_idx))

    if not auction_id:
        return {
            "status": "WAITING_INPUT",
            "expected_input": "SELECTION",
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": (
                "Quel appel d'offres souhaitez-vous consulter ?\n"
                "💡 _Tapez *mes enchères* pour voir la liste._"
            ),
            "ag_ui_component": None,
        }

    gw = AuctionGateway(mc_runtime)
    bids_result = await gw.get_auction_bids(auction_id=str(auction_id))

    bids = bids_result.get("bids") or bids_result.get("data") or []
    auction_info = bids_result.get("auction") or {}
    product = auction_info.get("product") or auction_info.get("product_name") or "Votre produit"
    status_raw = str(auction_info.get("status") or bids_result.get("auction_status") or "OPEN").upper()
    status_label = _status_label(status_raw, AUCTION_STATUS_MAP)

    lines = [
        f"📋 *Enchère — {product}*",
        f"Statut : {status_label}",
    ]

    if not bids:
        lines.append("\n📭 _Aucune offre reçue pour le moment._")
        if status_raw == "OPEN":
            lines.append("Les producteurs peuvent encore soumettre des offres.")
    else:
        lines.append(f"\n📥 *{len(bids)} offre{'s' if len(bids) > 1 else ''} reçue{'s' if len(bids) > 1 else ''} :*")
        mapping: Dict[str, str] = {}
        options: List[MenuOption] = []

        for i, bid in enumerate(bids, start=1):
            bid_id = str(bid.get("bid_id") or bid.get("id") or "")
            producer = bid.get("producer") or bid.get("producer_name") or "Producteur"
            price = bid.get("price") or bid.get("offered_price") or "?"
            bid_status = str(bid.get("status") or "PENDING").upper()
            bid_emoji = "🟡" if bid_status == "PENDING" else "✅" if bid_status == "ACCEPTED" else "🔴"

            label = f"{producer} — {price} FCFA"
            lines.append(f"\n*{i}.* {bid_emoji} {label}")
            mapping[str(i)] = bid_id
            options.append(MenuOption(index=str(i), label=label, value=bid_id))

        if status_raw == "OPEN":
            lines.append("\n_Répondez avec le *numéro* de l'offre pour désigner le gagnant._")

            wm = dict(state.get("working_memory") or {})
            wm["available_mapping_kind"] = "auction_bids"
            wm["winner_auction_id"] = str(auction_id)
            payload_out = dict(state.get("transaction_payload") or {})
            payload_out["auction_id"] = str(auction_id)
            payload_out.pop("selection_index", None)

            return {
                "status": "WAITING_INPUT",
                "expected_input": "SELECTION",
                "response_strategy": "SELECTION_MENU",
                "final_response": "\n".join(lines),
                "available_mapping": mapping,
                "current_goal": "BUYER_CHECK_AUCTION_STATUS",
                "transaction_payload": payload_out,
                "working_memory": wm,
                "ag_ui_component": None,
                "pending_menu": MenuRequest(
                    title=f"Offres — {product}",
                    options=options,
                    kind="auction_bids",
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

_YES_TOKENS = frozenset({
    "oui", "ok", "okay", "daccord", "d'accord", "c'est bon", "cest bon",
    "confirme", "confirmer", "je confirme", "valide", "valider", "go", "vasy",
    "parfait", "yes",
})
_NO_TOKENS = frozenset({
    "non", "annuler", "annule", "stop", "cancel", "quitter", "pas maintenant", "retour",
})


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


async def confirm_winner_selection(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    """L'acheteur a choisi une offre : afficher un récap + demander confirmation."""
    working = state.get("working_memory") or {}
    auction_id = working.get("winner_auction_id") or (state.get("transaction_payload") or {}).get("auction_id")
    mapping = state.get("available_mapping") or {}
    sel = _selection_index(state)

    bid_id = mapping.get(str(sel)) if sel is not None else None
    if not bid_id:
        return {
            "status": "WAITING_INPUT",
            "expected_input": "SELECTION",
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": "Indiquez le *numéro* de l'offre à retenir (ex: 1).",
            "ag_ui_component": None,
        }

    # Récap : re-fetch des offres pour retrouver producteur + prix de l'offre choisie.
    producer = "ce producteur"
    price_txt = ""
    product = "votre produit"
    if auction_id:
        gw = AuctionGateway(mc_runtime)
        try:
            detail = await gw.get_auction_bids(auction_id=str(auction_id))
            product = (detail.get("auction") or {}).get("product") or product
            for b in detail.get("bids") or []:
                if str(b.get("bid_id") or b.get("id")) == str(bid_id):
                    producer = b.get("producer") or b.get("producer_name") or producer
                    price = b.get("price") or b.get("offered_price")
                    if price is not None:
                        price_txt = f" à *{float(price):g} FCFA*"
                    break
        except Exception as exc:
            logger.warning("confirm_winner_selection: refetch failed: %s", exc)

    wm = dict(working)
    wm["available_mapping_kind"] = "confirm_winner"
    wm["pending_winner_bid"] = str(bid_id)

    return {
        "status": "WAITING_INPUT",
        "expected_input": "CONFIRMATION",
        "response_strategy": "ASK_MISSING_FIELD",
        "current_goal": "BUYER_CHECK_AUCTION_STATUS",
        "final_response": (
            f"🤝 Vous allez retenir l'offre de *{producer}*{price_txt} pour *{product}*.\n\n"
            "⚠️ Cette action *clôture l'enchère* et crée la commande.\n"
            "👉 Répondez *oui* pour confirmer, ou *non* pour annuler."
        ),
        "working_memory": wm,
        "ag_ui_component": None,
    }


async def finalize_winner(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    """Traite la réponse oui/non à la confirmation du gagnant."""
    working = state.get("working_memory") or {}
    bid_id = working.get("pending_winner_bid")
    event = str(state.get("interpreted_event") or "").upper().strip()
    text = str(state.get("normalized_text") or "").strip().lower()

    def _clear_wm() -> Dict[str, Any]:
        wm = dict(working)
        for k in ("available_mapping_kind", "pending_winner_bid", "winner_auction_id"):
            wm.pop(k, None)
        return wm

    is_no = event == "REJECT" or text in _NO_TOKENS
    is_yes = event == "CONFIRM" or text in _YES_TOKENS

    if not bid_id or is_no:
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": (
                "D'accord, aucune offre n'a été retenue. "
                "Tapez *mes enchères* pour revoir vos appels d'offres."
            ),
            "working_memory": _clear_wm(),
            "available_mapping": {},
            "ag_ui_component": None,
        }

    if not is_yes:
        # Réponse ambiguë : on redemande explicitement.
        return {
            "status": "WAITING_INPUT",
            "expected_input": "CONFIRMATION",
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": "Répondez *oui* pour confirmer le gagnant, ou *non* pour annuler.",
            "ag_ui_component": None,
        }

    gw = AuctionGateway(mc_runtime)
    try:
        result = await gw.select_winning_bid(bid_id=str(bid_id))
    except Exception as exc:
        logger.exception("finalize_winner: select_winning_bid failed: %s", exc)
        return {
            "status": "COMPLETED",
            "response_strategy": "ERROR",
            "final_response": "Impossible de valider le gagnant pour le moment. Réessayez dans un instant.",
            "working_memory": _clear_wm(),
            "ag_ui_component": None,
        }

    if not is_success_response(result):
        return {
            "status": "COMPLETED",
            "response_strategy": "ERROR",
            "final_response": result.get("message") or "Cette offre n'a pas pu être retenue.",
            "working_memory": _clear_wm(),
            "ag_ui_component": None,
        }

    summary = result.get("summary_buyer") or "🤝 Offre retenue ! La commande a été créée et le producteur informé."
    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": summary,
        "working_memory": _clear_wm(),
        "available_mapping": {},
        "transaction_payload": {"__reset__": True},
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

    detail = await _safe_gw_call(mc_runtime, "get_transaction_summary", order_id=first_order_id)
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
    mapping_kind = str(working.get("available_mapping_kind") or "").strip()

    # --- Machine à états : désignation du gagnant (prioritaire) ---
    # 1. On attend la confirmation oui/non d'un gagnant déjà choisi.
    if mapping_kind == "confirm_winner" or working.get("pending_winner_bid"):
        return await finalize_winner(state, mc_runtime)
    # 2. Un menu d'offres est affiché et l'acheteur sélectionne une offre.
    if mapping_kind == "auction_bids" and _selection_index(state) is not None:
        return await confirm_winner_selection(state, mc_runtime)

    if goal == "BUYER_CHECK_ORDER_STATUS":
        return await check_order_status(state, mc_runtime)
    if goal == "BUYER_CANCEL_ORDER":
        return await cancel_order(state, mc_runtime)
    if goal == "BUYER_LIST_AUCTIONS":
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
                MenuOption(index="1", label="📦 Voir mes commandes", value="BUYER_LIST_ORDERS"),
                MenuOption(index="2", label="🛒 Nouvelle commande", value="BUYER_ADD_TO_CART"),
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
