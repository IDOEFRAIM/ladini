"""Buyer negotiation gate — counter-offers, bid viewing, session lifecycle."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from agriconnect.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)
from agriconnect.graphs.agents.market_coach.services.domain.buyer_common import safe_call_tool
from agriconnect.graphs.agents.market_coach.utils import MarketRuntime, is_success_response

from .helpers import (
    NEGOTIATION_GOALS,
    logger,
    negotiation_action_menu,
    negotiation_choice_from_index,
    resolve_product,
    resolve_quantity,
)

# =====================================================================
# INTERNAL — build bids list UI (DRY, used in multiple branches)
# =====================================================================


def _build_bids_menu(
    bids: List[Dict[str, Any]],
    auction_id: str,
) -> Dict[str, Any]:
    """Build a selection menu from a list of bids. Returns a state patch."""
    mapping: Dict[str, str] = {}
    lines = ["📥 *Offres reçues :*"]
    options: List[Dict[str, str]] = []
    for i, b in enumerate(bids, start=1):
        bid_id = str(b.get("bid_id") or b.get("id") or "")
        producer = b.get("producer") or b.get("producer_name") or "Producteur"
        price = b.get("price") or b.get("offered_price") or "?"
        lines.append(f"\n*{i}. {producer}* — 💰 {price} CFA")
        options.append({"index": str(i), "label": f"{producer} — {price} CFA"})
        if bid_id:
            mapping[str(i)] = bid_id
    lines.append("\n_Répondez avec le numéro pour accepter une offre._")

    bids_text = "\n".join(lines)
    return {
        "status": "WAITING_INPUT",
        "expected_input": "SELECTION",
        "response_strategy": "SELECTION_MENU",
        "final_response": bids_text,
        "transaction_payload": {"resolved_id": None, "bid_id": None},
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Offres reçues",
            options=[
                MenuOption(index=o["index"], label=o["label"], value=mapping.get(o["index"]))
                for o in options
            ],
            kind="bid",
            metadata={"auction_id": str(auction_id)},
            preformatted_text=bids_text,
        ),
    }


async def _fetch_and_show_bids(
    mc_runtime: MarketRuntime,
    auction_id: str,
    nctx: Dict[str, Any],
    target_phase: str = "VIEWING_OFFERS",
) -> Dict[str, Any]:
    """Fetch auction bids and return a menu state patch or fallback to negotiation menu."""
    bids_res = await safe_call_tool(mc_runtime, "get_auction_bids", auction_id=str(auction_id))
    bids = bids_res.get("bids") or []

    if str(bids_res.get("status") or "").lower() != "success" or not bids:
        msg = bids_res.get("message") or "Aucune offre reçue pour l'instant."
        neg_menu = negotiation_action_menu(str(auction_id))
        return {
            "status": "WAITING_INPUT",
            "expected_input": "SELECTION",
            "response_strategy": "SELECTION_MENU",
            "final_response": msg,
            "transaction_payload": {"resolved_id": None},
            "negotiation_context": {**nctx, "phase": "NEGOTIATION_MENU"},
            "ag_ui_component": None,
            "pending_menu": neg_menu,
        }

    result = _build_bids_menu(bids, auction_id)
    result["negotiation_context"] = {**nctx, "phase": target_phase}
    return result


# =====================================================================
# PHASE: AWAIT_COUNTER_PRICE — buyer submits a new price
# =====================================================================


async def _handle_counter_price(
    mc_runtime: MarketRuntime,
    phone: str,
    payload: Dict[str, Any],
    nctx: Dict[str, Any],
    auction_id: str,
) -> Dict[str, Any]:
    """Process the counter-offer price submission."""
    price = payload.get("price_mentioned")
    if price in (None, "", 0):
        return {
            "status": "WAITING_INPUT",
            "expected_input": "PRICE",
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": "Quel est votre nouveau prix (FCFA) ?",
            "ag_ui_component": None,
        }

    upd = await safe_call_tool(
        mc_runtime,
        "update_negotiation_offer",
        buyer_phone=phone,
        negotiation_id=str(auction_id),
        new_price=price,
    )
    msg = upd.get("message") or "Offre mise à jour."
    neg_menu = negotiation_action_menu(str(auction_id))
    return {
        "status": "WAITING_INPUT",
        "expected_input": "SELECTION",
        "response_strategy": "SELECTION_MENU",
        "final_response": msg,
        "transaction_payload": {"resolved_id": None},
        "negotiation_context": {
            **nctx,
            "phase": "NEGOTIATION_MENU",
            "buyer_offer": upd.get("new_price") or nctx.get("buyer_offer"),
        },
        "ag_ui_component": None,
        "pending_menu": neg_menu,
    }


# =====================================================================
# PHASE: VIEWING_OFFERS — buyer selects a bid to accept
# =====================================================================


async def _handle_viewing_offers(
    mc_runtime: MarketRuntime,
    payload: Dict[str, Any],
    nctx: Dict[str, Any],
    auction_id: str,
) -> Dict[str, Any]:
    """Process bid selection or re-display bids."""
    bid_id = payload.get("bid_id")
    if bid_id:
        win = await safe_call_tool(mc_runtime, "select_winning_bid", bid_id=str(bid_id))
        if str(win.get("status") or "").lower() != "success":
            return {
                "status": "COMPLETED",
                "response_strategy": "ERROR",
                "final_response": win.get("message") or "Impossible de valider cette offre.",
                "transaction_payload": {"resolved_id": None},
                "ag_ui_component": None,
            }
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": win.get("summary_buyer") or "✅ Offre acceptée.",
            "current_goal": None,
            "transaction_payload": {"__reset__": True},
            "negotiation_context": {"__reset__": True},
            "ag_ui_component": None,
        }

    # No bid_id yet — fetch and show bids
    return await _fetch_and_show_bids(mc_runtime, auction_id, nctx, "VIEWING_OFFERS")


# =====================================================================
# PHASE: NEGOTIATION_MENU — action selection
# =====================================================================


async def _handle_negotiation_menu(
    mc_runtime: MarketRuntime,
    phone: str,
    payload: Dict[str, Any],
    state: Dict[str, Any],
    nctx: Dict[str, Any],
    auction_id: str,
) -> Dict[str, Any]:
    """Process action selection from the negotiation menu."""
    action = payload.get("resolved_id")
    if not action:
        selection_idx = payload.get("selection_index")
        mapped = negotiation_choice_from_index(selection_idx)
        if mapped:
            action = mapped
            payload["resolved_id"] = action
        if selection_idx not in (None, ""):
            payload.pop("selection_index", None)

    action = str(action).upper().strip() if action else ""

    if not action:
        msg = nctx.get("last_message") or state.get("final_response") or "Négociation en cours."
        neg_menu = negotiation_action_menu(str(auction_id))
        return {
            "status": "WAITING_INPUT",
            "expected_input": "SELECTION",
            "response_strategy": "SELECTION_MENU",
            "final_response": msg,
            "ag_ui_component": None,
            "pending_menu": neg_menu,
        }

    if action == "NEGOTIATION_VIEW_OFFERS":
        return await _fetch_and_show_bids(mc_runtime, auction_id, nctx, "VIEWING_OFFERS")

    if action == "NEGOTIATION_COUNTER":
        return {
            "status": "WAITING_INPUT",
            "expected_input": "PRICE",
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": "Quel est votre nouveau prix (FCFA) ?",
            "transaction_payload": {"resolved_id": None},
            "negotiation_context": {**nctx, "phase": "AWAIT_COUNTER_PRICE"},
            "ag_ui_component": None,
        }

    # NEGOTIATION_ABORT or unknown → close
    close_res = await safe_call_tool(
        mc_runtime,
        "close_negotiation_session",
        buyer_phone=phone,
        negotiation_id=str(auction_id),
        reason="buyer_abandoned",
    )
    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": close_res.get("message") or "Négociation annulée.",
        "current_goal": None,
        "transaction_payload": {"__reset__": True},
        "negotiation_context": {"__reset__": True},
        "ag_ui_component": None,
    }


# =====================================================================
# NEGOTIATION INITIATION — open a new negotiation session
# =====================================================================


async def _initiate_negotiation(
    mc_runtime: MarketRuntime,
    phone: str,
    product_name: str,
    quantity: Any,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Initiate a new negotiation session for a product."""
    # Resolve product reference
    ref = await _resolve_product_ref(mc_runtime, phone, product_name)
    if ref is None:
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": f"Aucun produit « {product_name} » n'est disponible pour négocier.",
            "ag_ui_component": None,
        }

    try:
        offer = float(payload.get("price_mentioned"))
    except (TypeError, ValueError):
        offer = 0.0

    res = await safe_call_tool(
        mc_runtime,
        "initiate_negotiation_session",
        buyer_phone=phone,
        product_id=ref["product_id"],
        offered_price=offer,
        quantity=quantity,
    )

    if str(res.get("status") or "").upper() not in {"PENDING", "SUCCESS"}:
        return {
            "status": "COMPLETED",
            "response_strategy": "ERROR",
            "final_response": res.get("message") or "Impossible d'ouvrir la négociation.",
            "fallback_recommendations": res.get("fallback") or [],
            "ag_ui_component": None,
        }

    seller_min = res.get("seller_minimum")
    gap = res.get("price_gap")
    recos = []
    if isinstance(gap, (int, float)) and isinstance(seller_min, (int, float)) and gap > 0:
        suggested = round((offer + float(seller_min)) / 2.0, 2)
        recos = [{"type": "suggested_price", "value": suggested,
                  "label": f"Proposer un prix médian de {suggested} FCFA"}]

    neg_init_menu = MenuRequest(
        title="Négociation en cours",
        options=negotiation_action_menu(str(res.get("negotiation_id") or "")).options,
        kind="negotiation_action",
        metadata={"session_id": res.get("negotiation_id")},
    )
    return {
        "status": "WAITING_INPUT",
        "expected_input": "SELECTION",
        "response_strategy": "SELECTION_MENU",
        "final_response": res.get("message"),
        "negotiation_context": {
            "session_id": res.get("negotiation_id"),
            "auction_id": res.get("auction_id"),
            "product_id": res.get("product_id"),
            "producer_id": res.get("producer_id"),
            "buyer_offer": res.get("buyer_offer"),
            "seller_minimum": res.get("seller_minimum"),
            "status": "PENDING",
            "phase": "NEGOTIATION_MENU",
            "last_message": res.get("message"),
        },
        "fallback_recommendations": recos,
        "ag_ui_component": None,
        "pending_menu": neg_init_menu,
    }


async def _resolve_product_ref(
    mc_runtime: MarketRuntime,
    phone: str,
    product_name: str,
) -> Optional[Dict[str, Any]]:
    """Resolve a product name to a catalog reference."""
    if not product_name:
        return None
    res = await safe_call_tool(mc_runtime, "search_products", product=str(product_name), phone=str(phone))
    if not is_success_response(res):
        return None
    results = res.get("results") or (res.get("data") or {}).get("results") or []
    if not results:
        return None
    best = results[0]
    return {
        "product_id": str(best.get("id")),
        "crop_cycle_id": best.get("crop_cycle_id"),
        "name": str(best.get("name") or product_name),
        "price": float(best.get("price") or 0.0),
        "unit": str(best.get("unit") or "KG").upper(),
        "vendor": best.get("vendor"),
        "vendor_name": best.get("vendor_name") or best.get("producer_name") or best.get("vendor"),
        "producer_id": best.get("producer_id") or best.get("vendor_id"),
    }


# =====================================================================
# MAIN ENTRY POINT — negotiation_gate node
# =====================================================================


async def negotiation_gate(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Negotiation gate — deterministic phase-based routing."""
    goal = (state.get("current_goal") or "").upper()
    payload: Dict[str, Any] = dict(state.get("transaction_payload") or {})
    phone = str(state.get("user_phone") or "")
    stable_entities = dict(state.get("stable_entities") or {})

    nctx: Dict[str, Any] = dict(state.get("negotiation_context") or {})
    nphase = str(nctx.get("phase") or "NEGOTIATION_MENU").upper().strip()
    auction_id = nctx.get("auction_id") or nctx.get("session_id")

    product_name = resolve_product(payload, stable_entities, state)
    quantity = resolve_quantity(payload, stable_entities)

    if goal not in NEGOTIATION_GOALS:
        return {"status": "PLANNING", "ag_ui_component": None}

    # Route by negotiation phase
    if auction_id and nphase == "AWAIT_COUNTER_PRICE":
        return await _handle_counter_price(mc_runtime, phone, payload, nctx, auction_id)

    if auction_id and nphase == "VIEWING_OFFERS":
        return await _handle_viewing_offers(mc_runtime, payload, nctx, auction_id)

    if auction_id and nphase == "NEGOTIATION_MENU":
        return await _handle_negotiation_menu(mc_runtime, phone, payload, state, nctx, auction_id)

    # No active session — initiate new negotiation
    if not product_name or payload.get("price_mentioned") in (None, "", 0):
        return {
            "status": "WAITING_INPUT",
            "expected_input": "PRODUCT" if not product_name else "PRICE",
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": "Quel produit souhaitez-vous négocier et à quel prix ?",
            "ag_ui_component": None,
        }

    return await _initiate_negotiation(mc_runtime, phone, str(product_name), quantity, payload)


__all__ = ["negotiation_gate"]
