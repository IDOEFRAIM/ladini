"""Buyer procurement — auction creation escalation + own-auctions listing."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from datetime import datetime, timedelta

from agriconnect.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)
from agriconnect.graphs.agents.market_coach.services.domain.buyer_common import (
    safe_call_tool,
    with_support_footer,
)
from agriconnect.graphs.agents.market_coach.services.mcp.gateway import AuctionGateway
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    ensure_dict,
    is_success_response,
)

from .cart import cart_management

from .helpers import (
    CONFIRM_KEYWORDS,
    DECLINE_KEYWORDS,
    ESCALATE_KEYWORDS,
    infer_product_from_text,
    logger,
    phone_missing_error,
    resolve_product,
    resolve_quantity,
    resolve_unit,
)

# =====================================================================
# PROCUREMENT ESCALATION BUILDER
# =====================================================================


def build_procurement_escalation(
    payload: Dict[str, Any],
    working_memory: Dict[str, Any],
    product_name: Optional[str],
    unit: Optional[str],
    message: str,
) -> Dict[str, Any]:
    """Build state patch that escalates to the AUCTION_CREATE form."""
    next_payload = dict(payload)
    if product_name:
        next_payload.setdefault("product", product_name)
    next_payload.pop("selection_index", None)
    next_payload.pop("selected_value", None)
    if next_payload.pop("_auto_quantity_fill", False):
        next_payload.pop("quantity_mentioned", None)
        next_payload.pop("quantity", None)

    form_data: Dict[str, Any] = {}
    if next_payload.get("product") not in (None, "", [], {}):
        form_data["product"] = next_payload.get("product")
    if unit not in (None, "", [], {}):
        form_data["unit_mentioned"] = unit

    # Prefill default deadline (+30 days) but keep it editable
    if not next_payload.get("deadline") and not next_payload.get("deadline_date"):
        default_deadline = (datetime.utcnow() + timedelta(days=30)).date().isoformat()
        form_data["deadline"] = default_deadline

    required_order = ("product", "price_mentioned", "quantity_mentioned", "deadline")
    missing_fields = [k for k in required_order if form_data.get(k) in (None, "", 0, [], {})]
    last_missing_field = missing_fields[0] if missing_fields else None
    expected_input = last_missing_field.upper() if last_missing_field else "NONE"

    wm = dict(working_memory)
    wm["active_goal"] = "PROCUREMENT_CREATE_REQUEST"
    wm["locked_intent"] = "PROCUREMENT_CREATE_REQUEST"
    for key in ("buyer_request_waiting_choice", "buyer_request_catalog_checked", "buyer_request_last_product"):
        wm.pop(key, None)

    return {
        "status": "PLANNING",
        "current_goal": "PROCUREMENT_CREATE_REQUEST",
        "goal_status": "ACTIVE",
        "response_strategy": "SUCCESS",
        "final_response": message,
        "working_memory": wm,
        "transaction_payload": next_payload,
        "active_form": "AUCTION_CREATE",
        "form_data": form_data,
        "form_step": None,
        "missing_fields": missing_fields,
        "last_missing_field": last_missing_field,
        "expected_input": expected_input,
        "vendor_selection_context": {"__reset__": True},
        "ag_ui_component": None,
    }


# =====================================================================
# BUYER REQUEST RESOLVER — catalog search then procurement escalation
# =====================================================================


async def buyer_request_resolver(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Search catalog first, then escalate to procurement on confirmation."""
    from agriconnect.graphs.agents.market_coach.services.domain.cart_service import (
        CartDomainService,
    )

    payload: Dict[str, Any] = dict(state.get("transaction_payload") or {})
    if not payload and state.get("extracted_entities"):
        payload = dict(state.get("extracted_entities") or {})

    phone = str(state.get("user_phone") or "")
    if not phone:
        return phone_missing_error()

    vendor_ctx = state.get("vendor_selection_context")
    vendor_ctx_active = bool(vendor_ctx) and not (isinstance(vendor_ctx, dict) and vendor_ctx.get("__reset__"))
    if vendor_ctx_active:
        raw_selection = payload.get("selection_index")
        if raw_selection is None:
            raw_selection = (state.get("extracted_entities") or {}).get("selection_index")
        if raw_selection is None:
            raw_text = str(state.get("normalized_text") or state.get("user_query") or "").strip()
            if raw_text.isdigit():
                raw_selection = raw_text

        if raw_selection is not None:
            next_payload = dict(payload)
            next_payload["selection_index"] = raw_selection
            next_state = dict(state)
            next_state["current_goal"] = "BUYER_ADD_TO_CART"
            next_state["transaction_payload"] = next_payload
            return await cart_management(next_state, mc_runtime)

    stable_entities = state.get("stable_entities") or {}
    working_memory = dict(state.get("working_memory") or {})
    normalized_text = str(state.get("normalized_text") or state.get("user_query") or "").strip().lower()
    cart_service = CartDomainService(mc_runtime)

    current_goal = str(state.get("current_goal") or "").upper()

    # --- Entity resolution ---
    product_name = resolve_product(payload, stable_entities, state)
    inferred_from_text = bool(payload.pop("_product_from_text", False))
    unit = resolve_unit(payload, stable_entities)
    payload.setdefault("product", product_name)

    text_has_digits = any(ch.isdigit() for ch in normalized_text)
    reset_quantity = inferred_from_text and not text_has_digits
    if reset_quantity:
        payload.pop("quantity_mentioned", None)
        payload.pop("quantity", None)

    def _escalate(message: str, unit_hint: Optional[str] = None) -> Dict[str, Any]:
        return build_procurement_escalation(payload, working_memory, product_name, unit_hint or unit, message)

    _ESCALATION_MSG = (
        "Très bien, lançons un appel d'offres. "
        "J'aurai besoin du prix minimum, de la quantité souhaitée et d'une date limite."
    )

    # --- Direct escalation triggers ---
    if normalized_text in ESCALATE_KEYWORDS and product_name:
        return _escalate(_ESCALATION_MSG)

    if current_goal == "PROCUREMENT_CREATE_REQUEST" and state.get("active_form") == "AUCTION_CREATE":
        return _escalate(_ESCALATION_MSG)

    # --- Waiting-choice state (no catalog stock, user asked if they want procurement) ---
    waiting_choice = bool(working_memory.get("buyer_request_waiting_choice"))
    interpreted_event = str(state.get("interpreted_event") or "").upper()

    if waiting_choice:
        confirm_signal = normalized_text in CONFIRM_KEYWORDS or interpreted_event == "CONFIRM"
        reject_signal = normalized_text in DECLINE_KEYWORDS or interpreted_event == "REJECT"

        if confirm_signal or normalized_text in ESCALATE_KEYWORDS:
            return _escalate(_ESCALATION_MSG)
        if reject_signal:
            wm = dict(working_memory)
            for key in ("buyer_request_waiting_choice", "buyer_request_catalog_checked", "buyer_request_last_product"):
                wm.pop(key, None)
            return {
                "status": "COMPLETED",
                "response_strategy": "SUCCESS",
                "final_response": "Très bien, je reste à votre disposition si vous souhaitez tenter un appel d'offres plus tard.",
                "working_memory": wm,
                "transaction_payload": {"__reset__": True},
                "ag_ui_component": None,
            }

    # --- Ask for product if unknown ---
    if not product_name:
        return {
            "status": "WAITING_INPUT",
            "expected_input": "PRODUCT",
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": "Quel produit recherchez-vous ?",
            "transaction_payload": payload,
            "ag_ui_component": None,
            "working_memory": working_memory,
        }

    # --- Catalog lookup ---
    allow_stable_quantity = not reset_quantity
    quantity = resolve_quantity(
        payload,
        stable_entities,
        allow_stable_fallback=allow_stable_quantity,
    )
    vendors, has_multiple = await cart_service.resolve_product_vendors(phone, str(product_name))

    if vendors:
        extra_context = {"requested_quantity": quantity, "requested_unit": unit}
        menu_patch, _menu = cart_service.build_product_selection_menu(
            str(product_name),
            vendors,
            extra_context=extra_context,
            post_hint="💡 Si aucune offre ne vous convient, répondez *appel* pour lancer une demande spéciale aux producteurs.",
        )
        menu_patch["transaction_payload"] = payload
        wm = dict(working_memory)
        wm.update({
            "buyer_request_catalog_checked": True,
            "buyer_request_last_product": product_name,
        })
        menu_patch["working_memory"] = wm
        menu_patch["current_goal"] = "BUYER_REQUEST"
        return menu_patch

    # --- No stock: propose procurement ---
    wm = dict(working_memory)
    wm.update({
        "buyer_request_catalog_checked": True,
        "buyer_request_waiting_choice": True,
        "buyer_request_last_product": product_name,
    })
    msg = (
        f"Désolé, aucune offre n'est disponible pour « {product_name} » dans notre catalogue. "
        "Souhaitez-vous que je demande aux producteurs si quelqu'un peut fournir ? (Oui/Non)"
    )
    return {
        "status": "WAITING_INPUT",
        "expected_input": "CONFIRMATION",
        "response_strategy": "ASK_MISSING_FIELD",
        "final_response": msg,
        "ag_ui_component": None,
        "transaction_payload": payload,
        "working_memory": wm,
    }


# =====================================================================
# OWN AUCTIONS — list buyer's open procurement requests
# =====================================================================


async def resolve_own_auctions(
    mc_runtime: MarketRuntime,
    phone: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Retrieve open auctions created by the buyer."""
    if not phone:
        return phone_missing_error()

    kwargs: Dict[str, Any] = {"phone": str(phone), "status": "OPEN", "view_mode": "MY_OWN"}
    product = payload.get("product") or payload.get("product_name")
    if product:
        kwargs["product_name"] = str(product)

    logger.info("resolve_own_auctions: calling get_auctions with %s", kwargs)
    auction_gw = AuctionGateway(mc_runtime)
    result = await auction_gw.search_open_auctions(**kwargs)

    if not is_success_response(result) or int(result.get("count") or 0) == 0:
        msg = result.get("message") or "Vous n'avez aucun appel d'offres ouvert pour l'instant. Si vous pensez que c'est une erreur, veuillez réessayer."
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": msg,
            "ag_ui_component": None,
        }

    mapping = result.get("mapping") or {}
    menu = result.get("formatted_menu") or "Vos appels d'offres ouverts ont été trouvés."
    candidates = [str(d.get("product")) for d in (result.get("data") or [])]

    return {
        "status": "WAITING_INPUT",
        "expected_input": "SELECTION",
        "working_memory": {"auction_menu": menu},
        "response_strategy": "SELECTION_MENU",
        "final_response": menu,
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Vos appels d'offres",
            options=[
                MenuOption(index=str(i), label=c, value=mapping.get(str(i)))
                for i, c in enumerate(candidates, start=1)
            ],
            kind="auction",
            preformatted_text=menu,
        ),
    }


# =====================================================================
# RECEIVED BIDS — bids placed on buyer's auctions
# =====================================================================


async def resolve_received_bids(
    mc_runtime: MarketRuntime,
    phone: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Retrieve bids deposited on buyer's auctions."""
    kwargs: Dict[str, Any] = {"status": "OPEN"}
    if not phone:
        return phone_missing_error()
    kwargs["phone"] = str(phone)

    auction_gw = AuctionGateway(mc_runtime)
    result = await auction_gw.get_auctions_bids(**kwargs)

    if not is_success_response(result):
        msg = result.get("message") or "Impossible de charger les offres reçues."
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": msg,
            "ag_ui_component": None,
        }

    data = result.get("data") or []
    if not data:
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": "Aucune offre n'a encore été déposée sur vos enchères.",
            "ag_ui_component": None,
        }

    mapping: Dict[str, str] = {}
    lines = ["📥 *Offres reçues sur vos enchères :*"]
    for i, bid in enumerate(data, start=1):
        bid_id = str(bid.get("bid_id") or bid.get("id") or "")
        producer_name = bid.get("producer_name") or bid.get("seller_name") or "Producteur"
        price = bid.get("offered_price") or bid.get("price") or "?"
        product_name = bid.get("product") or bid.get("product_name") or "?"
        status = bid.get("status") or "PENDING"
        lines.append(f"\n*{i}. {producer_name}* — {product_name}\n💰 {price} FCFA — Statut: {status}")
        mapping[str(i)] = bid_id

    menu = result.get("formatted_menu") or "\n".join(lines)
    candidates = [
        f"{b.get('producer_name') or b.get('seller_name') or 'Producteur'} ({b.get('product') or b.get('product_name') or 'Produit'})"
        for b in data
    ]
    return {
        "status": "WAITING_INPUT",
        "expected_input": "SELECTION",
        "working_memory": {"bids_menu": menu, "bids_cache": data},
        "response_strategy": "SELECTION_MENU",
        "final_response": menu,
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Offres reçues",
            options=[
                MenuOption(index=str(i), label=c, value=mapping.get(str(i)))
                for i, c in enumerate(candidates, start=1)
            ],
            kind="bid",
            preformatted_text=menu,
        ),
    }


# =====================================================================
# BID PICK — resolve bid_id for acceptance
# =====================================================================


async def resolve_buyer_bid_pick(
    mc_runtime: MarketRuntime,
    phone: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Resolve the bid_id required for acceptance from selection index."""
    if payload.get("bid_id"):
        return {"status": "PLANNING", "ag_ui_component": None}

    kwargs: Dict[str, Any] = {"status": "OPEN"}
    if phone:
        kwargs["phone"] = str(phone)

    auction_gw = AuctionGateway(mc_runtime)
    data = (await auction_gw.get_auctions_bids(**kwargs)).get("data") or []

    if not isinstance(data, list) or not data:
        return {
            "status": "ERROR",
            "validation_errors": ["no_open_bids"],
            "response_strategy": "ERROR",
            "final_response": "Aucune offre disponible à accepter.",
            "ag_ui_component": None,
        }

    idx = payload.get("selection_index")
    selected_value = payload.get("selected_value")

    chosen = None
    if isinstance(idx, int) and 1 <= idx <= len(data):
        chosen = data[idx - 1]
    elif selected_value:
        target = str(selected_value).strip().lower()
        chosen = next(
            (b for b in data if b and (
                target in str(b.get("producer_name")).lower()
                or target in str(b.get("seller_name")).lower()
            )),
            None,
        )

    if chosen:
        bid_id = chosen.get("bid_id") or chosen.get("id")
        if not bid_id:
            return {
                "status": "ERROR",
                "validation_errors": ["bid_not_resolved"],
                "response_strategy": "ERROR",
                "final_response": "Identifiant de l'offre introuvable sur l'élément sélectionné.",
                "ag_ui_component": None,
            }
        new_payload = dict(payload)
        new_payload["bid_id"] = str(bid_id)
        new_payload.pop("selection_index", None)
        new_payload.pop("selected_value", None)
        return {"status": "PLANNING", "transaction_payload": new_payload, "ag_ui_component": None}

    # Fallback: re-display bids
    return await resolve_received_bids(mc_runtime, phone, payload)


__all__ = [
    "build_procurement_escalation",
    "buyer_request_resolver",
    "resolve_own_auctions",
    "resolve_received_bids",
    "resolve_buyer_bid_pick",
]
