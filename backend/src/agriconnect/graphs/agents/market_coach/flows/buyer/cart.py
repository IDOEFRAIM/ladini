"""Buyer cart management — add to cart, view cart, vendor selection."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from agriconnect.graphs.agents.market_coach.services.domain.buyer_common import safe_call_tool
from agriconnect.graphs.agents.market_coach.services.domain.cart_service import CartDomainService
from agriconnect.graphs.agents.market_coach.utils import MarketRuntime, is_success_response

from .helpers import (
    capture_cart_draft,
    detect_cart_action,
    infer_product_from_text,
    logger,
    resolve_product,
    resolve_quantity,
)
from .preorder import create_preorder

# =====================================================================
# CART MANAGEMENT NODE
# =====================================================================


async def cart_management(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Cart management node — handles add-to-cart, view, and vendor selection.

    Deterministic routing based on goal and available entities:
    - BUYER_VIEW_CART → render cart
    - BUYER_ADD_TO_CART → resolve product, check vendors, add line
    - Free-text preorder keyword → delegate to preorder workflow
    """
    goal = (state.get("current_goal") or "").upper()

    payload: Dict[str, Any] = dict(state.get("transaction_payload") or {})
    if not payload and state.get("extracted_entities"):
        payload = dict(state.get("extracted_entities"))

    phone = str(state.get("user_phone") or "")
    cart: List[Dict[str, Any]] = list(state.get("active_cart") or [])
    stable_entities = state.get("stable_entities") or {}
    cart_service = CartDomainService(mc_runtime)

    def _with_base(extra: Dict[str, Any]) -> Dict[str, Any]:
        base: Dict[str, Any] = {"active_cart": cart}
        if "final_response" not in extra:
            base["final_response"] = None
        if "ag_ui_component" not in extra:
            base["ag_ui_component"] = None
        base.update(extra)
        return base

    # --- Free-text preorder trigger ---
    cart_action = detect_cart_action(state)
    if cart_action == "PREORDER" and cart:
        logger.info("cart_management: preorder command detected via free text")
        next_state = dict(state)
        next_state["current_goal"] = "BUYER_PREORDER_INIT"
        next_state["active_cart"] = cart
        next_state["transaction_payload"] = dict(payload)
        preorder_patch = await create_preorder(next_state, mc_runtime)
        return _with_base(preorder_patch)

    # --- Resolve entities ---
    product_name = resolve_product(payload, stable_entities, state)
    if product_name:
        payload.setdefault("product", product_name)

    quantity = resolve_quantity(payload, stable_entities)

    # --- VIEW CART ---
    if goal == "BUYER_VIEW_CART":
        meta = CartDomainService.recompute_cart_meta(cart)
        pending_draft = state.get("draft_payload")
        if not pending_draft or pending_draft.get("__reset__"):
            pending_draft = state.get("suspended_payload")
        render = cart_service.render_cart_menu(cart, meta, pending_draft=pending_draft)
        return _with_base({
            "status": "COMPLETED",
            "response_strategy": "SELECTION_MENU" if cart else "SUCCESS",
            "preorder_workflow": {"phase": "CART"},
            "cart_meta": meta,
            **render,
        })

    # --- Guard: insufficient info for non-view goals ---
    if goal not in {"BUYER_ADD_TO_CART", "BUYER_VIEW_CART"} and not (product_name and quantity not in (None, "", 0)):
        return _with_base({
            "status": "PLANNING",
            "final_response": "Comment puis-je vous aider avec votre panier ?",
            "ag_ui_component": None,
        })

    # --- Missing product or quantity ---
    if not product_name or quantity in (None, "", 0):
        draft_snapshot = capture_cart_draft(state, payload)

        if product_name or quantity not in (None, "", 0):
            draft_item = {
                "product_id": "DRAFT",
                "name": str(product_name) if product_name else "EN ATTENTE",
                "quantity": float(quantity) if quantity not in (None, "", 0) else 0.0,
                "unit": str(payload.get("unit_mentioned") or "KG"),
                "price": 0.0,
                "line_total": 0.0,
                "status": "DRAFT",
            }
            cart = [c for c in cart if c.get("status") != "DRAFT" and c.get("product_id") != "DRAFT"]
            cart.append(draft_item)

        missing_field = "PRODUCT" if not product_name else "QUANTITY"
        prompt = (
            "Quel produit souhaitez-vous ajouter au panier ?"
            if not product_name
            else f"Quelle quantité de {product_name} souhaitez-vous ?"
        )

        vendor_ctx = state.get("vendor_selection_context")
        return _with_base({
            "status": "WAITING_INPUT",
            "expected_input": missing_field,
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": prompt,
            "transaction_payload": payload,
            "draft_payload": draft_snapshot or state.get("draft_payload") or payload,
            "vendor_selection_context": vendor_ctx if vendor_ctx else state.get("vendor_selection_context"),
            "ag_ui_component": None,
        })

    # --- VENDOR SELECTION: check if returning from vendor menu ---
    vendor_ctx = state.get("vendor_selection_context")
    vendor_ctx_active = bool(vendor_ctx) and not (isinstance(vendor_ctx, dict) and vendor_ctx.get("__reset__"))
    if vendor_ctx_active:
        vendor_ctx_payload: Dict[str, Any] = dict(vendor_ctx or {})
        chosen_vendor = vendor_ctx_payload.get("chosen_vendor")
        selection_idx = payload.get("selection_index")

        if selection_idx is not None:
            vendors_list = vendor_ctx_payload.get("vendors") or []
            try:
                idx = int(selection_idx) - 1
            except (TypeError, ValueError):
                idx = -1
            if 0 <= idx < len(vendors_list):
                chosen_vendor = vendors_list[idx]
                vendor_ctx_payload["chosen_vendor"] = chosen_vendor
                payload.pop("selection_index", None)
                if chosen_vendor:
                    payload["product"] = chosen_vendor.get("name") or product_name
                    product_name = payload["product"]

                requested_qty = vendor_ctx_payload.get("requested_quantity")
                requested_unit = vendor_ctx_payload.get("requested_unit")
                if quantity in (None, "", 0) and requested_qty not in (None, "", 0):
                    payload["quantity_mentioned"] = requested_qty
                    quantity = requested_qty
                if requested_unit:
                    payload.setdefault("unit_mentioned", requested_unit)
            else:
                return _with_base({
                    "status": "WAITING_INPUT",
                    "expected_input": "SELECTION",
                    "response_strategy": "ASK_MISSING_FIELD",
                    "final_response": "Numéro invalide. Choisissez un producteur dans la liste ci-dessus, ou tapez *annuler*.",
                    "ag_ui_component": None,
                })

        # Vendor chosen + quantity available → add to cart directly
        if chosen_vendor is not None and product_name and quantity not in (None, "", 0):
            return _with_base(
                await cart_service.add_to_cart_with_ref(
                    phone, str(product_name), quantity, chosen_vendor, cart, state,
                )
            )

    # --- MULTI-VENDOR RESOLUTION ---
    vendors, has_multiple = await cart_service.resolve_product_vendors(phone, str(product_name))

    if not vendors:
        return _with_base({
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": f"Désolé, le produit « {product_name} » n'est pas disponible dans notre catalogue. Souhaitez-vous voir une autre catégorie ?",
            "transaction_payload": {"__reset__": True},
            "negotiation_context": {"__reset__": True},
            "current_goal": None,
            "draft_payload": {"__reset__": True},
            "vendor_selection_context": {"__reset__": True},
            "expected_input": None,
            "ag_ui_component": None,
        })

    if has_multiple:
        extra_context = {
            "requested_quantity": quantity,
            "requested_unit": payload.get("unit_mentioned") or payload.get("unit"),
        }
        state_patch, _menu = cart_service.build_product_selection_menu(
            str(product_name), vendors, extra_context=extra_context,
        )
        return _with_base(state_patch)

    # Single vendor — add directly
    ref = vendors[0]
    return _with_base(
        await cart_service.add_to_cart_with_ref(phone, str(product_name), quantity, ref, cart, state)
    )


__all__ = ["cart_management"]
