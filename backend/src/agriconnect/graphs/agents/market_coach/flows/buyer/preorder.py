"""Buyer preorder workflow — draft → preflight recap → confirmation."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from agriconnect.graphs.agents.market_coach.flows.common.menu_contracts import MenuRequest
from agriconnect.graphs.agents.market_coach.services.domain.cart_service import (
    CartDomainService,
    SOURCE_TYPE_LABELS,
)
from agriconnect.graphs.agents.market_coach.services.mcp.gateway import PreorderGateway
from agriconnect.graphs.agents.market_coach.utils import MarketRuntime, is_success_response

from .helpers import (
    PREORDER_ACTION_OPTIONS,
    PREORDER_GOALS,
    clear_active_goal,
    logger,
    preorder_action_menu,
    preorder_choice_from_index,
    render_interactive_menu,
)

# =====================================================================
# PRE-FLIGHT RECAP
# =====================================================================


def build_preflight_recap(cart: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    """Build the pre-flight recap before final confirmation."""
    lines = ["📋 *Récapitulatif de votre précommande :*\n"]
    filtered_items: List[Dict[str, Any]] = []
    for item in cart:
        try:
            qty_val = float(item.get("quantity")) or None
        except (TypeError, ValueError):
            qty_val = None
        if qty_val is None or qty_val <= 0:
            continue
        filtered_items.append({"_quantity_val": qty_val, **item})

    for i, item in enumerate(filtered_items, start=1):
        source_label = SOURCE_TYPE_LABELS.get(
            str(item.get("source_type") or "DIRECT").upper(), "Catalogue"
        )
        vendor = item.get("vendor_name") or "—"
        qty = item.get("_quantity_val") or item.get("quantity")
        try:
            price_val = float(item.get("price") or 0.0)
        except (TypeError, ValueError):
            price_val = 0.0
        line_total = item.get("line_total")
        if line_total in (None, ""):
            line_total = round(float(qty or 0.0) * price_val, 2)
        lines.append(
            f"*{i}. {item.get('name')}*\n"
            f"   Quantité : {qty} {item.get('unit')}\n"
            f"   Prix unitaire : {item.get('price')} FCFA\n"
            f"   Sous-total : *{line_total} FCFA*\n"
            f"   Producteur : {vendor}\n"
            f"   Source : {source_label}"
        )
    lines.append(f"\n💰 *TOTAL : {meta.get('total_amount')} {meta.get('currency')}*")
    lines.append("\n_Que souhaitez-vous faire ?_")
    lines.append(render_interactive_menu(PREORDER_ACTION_OPTIONS))
    lines.append("\n👉 Répondez avec le *numéro* (1, 2 ou 3) ou tapez *confirmer* / *annuler*.")
    return "\n".join(lines)


# =====================================================================
# PREORDER PHASE TRANSITION HELPER
# =====================================================================


async def update_preorder_phase(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Optional[Dict[str, Any]]:
    """Force deterministic preorder phase transitions.

    Ensures CART ↔ PREORDER_DRAFTED ↔ CONFIRMED stay synchronized with goals.
    Returns a state patch or None if no transition is needed.
    """
    from .helpers import CART_GOALS

    goal = str(state.get("current_goal") or "").upper().strip()
    preorder_flow: Dict[str, Any] = dict(state.get("preorder_workflow") or {})
    phase = str(preorder_flow.get("phase") or "CART").upper().strip()
    working_snapshot = (state.get("working_memory") or {}).get("last_active_cart")
    cart_has_items = bool(state.get("active_cart") or working_snapshot)

    def _phase_patch(new_phase: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        p: Dict[str, Any] = {"phase": new_phase}
        if preorder_flow.get("preorder_id"):
            p["preorder_id"] = preorder_flow.get("preorder_id")
        if extra:
            p.update(extra)
        return {"preorder_workflow": p}

    if goal in CART_GOALS and phase != "CART":
        logger.info("update_preorder_phase: normalising back to CART phase")
        return _phase_patch("CART")

    if goal == "BUYER_PREORDER_INIT" and phase == "CART":
        logger.info("update_preorder_phase: INIT from CART -> draft")
        return await create_preorder(state, mc_runtime)

    if goal == "BUYER_PREORDER_CONFIRM":
        if phase == "CART" and cart_has_items:
            logger.info("update_preorder_phase: CONFIRM requested while CART — auto-drafting first")
            draft_updates = await create_preorder(state, mc_runtime)
            draft_flow = dict(draft_updates.get("preorder_workflow") or {})
            draft_phase = str(draft_flow.get("phase") or "").upper()
            if draft_phase != "PREORDER_DRAFTED":
                return draft_updates

            synthetic_state = dict(state)
            synthetic_state["preorder_workflow"] = draft_flow
            synthetic_payload = dict(
                draft_updates.get("transaction_payload") or state.get("transaction_payload") or {}
            )
            synthetic_payload["resolved_id"] = "PREORDER_CONFIRM"
            synthetic_payload.pop("selection_index", None)
            synthetic_payload.pop("selected_value", None)
            synthetic_state["transaction_payload"] = synthetic_payload
            synthetic_state["active_cart"] = draft_updates.get("active_cart", state.get("active_cart"))

            confirm_updates = await create_preorder(synthetic_state, mc_runtime)
            if "preorder_workflow" not in confirm_updates:
                confirm_updates["preorder_workflow"] = draft_flow
            return confirm_updates

        if phase == "PREORDER_DRAFTED":
            logger.info("update_preorder_phase: CONFIRM from PREORDER_DRAFTED -> confirm")
            next_state = dict(state)
            next_payload = dict(next_state.get("transaction_payload") or {})
            next_payload["resolved_id"] = "PREORDER_CONFIRM"
            next_payload.pop("selection_index", None)
            next_payload.pop("selected_value", None)
            next_state["transaction_payload"] = next_payload
            return await create_preorder(next_state, mc_runtime)

    return None


# =====================================================================
# MAIN PREORDER WORKFLOW (3-PHASE)
# =====================================================================


async def create_preorder(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Preorder workflow:
    1. CART → PREORDER_DRAFTED : create MCP draft + show preflight recap.
    2. PREORDER_DRAFTED + CONFIRM → CONFIRMED : confirm the preorder.
    3. Secondary actions (ADD_MORE, CANCEL).
    """
    goal = str(state.get("current_goal") or "").upper()
    payload: Dict[str, Any] = dict(state.get("transaction_payload") or {})
    phone = str(state.get("user_phone") or "")
    working_snapshot = (state.get("working_memory") or {}).get("last_active_cart")
    cart_source = state.get("active_cart") or working_snapshot or []
    cart: List[Dict[str, Any]] = list(cart_source)
    preorder_flow: Dict[str, Any] = dict(state.get("preorder_workflow") or {})
    phase = str(preorder_flow.get("phase") or "CART").upper().strip()

    # --- Resolve action from selection index ---
    resolved_id = payload.get("resolved_id")
    if not resolved_id:
        selection_idx = payload.get("selection_index")
        mapped_choice = preorder_choice_from_index(selection_idx)
        if mapped_choice:
            resolved_id = mapped_choice
            payload["resolved_id"] = resolved_id
        if selection_idx not in (None, ""):
            payload.pop("selection_index", None)
    if goal == "BUYER_PREORDER_CONFIRM" and not resolved_id:
        payload["resolved_id"] = "PREORDER_CONFIRM"
        resolved_id = "PREORDER_CONFIRM"
    if goal == "BUYER_CART_RESET" and not resolved_id:
        payload["resolved_id"] = "PREORDER_CANCEL"
        resolved_id = "PREORDER_CANCEL"

    # --- CANCEL ---
    if str(resolved_id).upper() == "PREORDER_CANCEL":
        logger.info("create_preorder: cancel requested — returning to CART")
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": "↩️ Précommande annulée. Votre panier est toujours disponible.",
            "preorder_workflow": {"__reset__": True, "phase": "CART"},
            "current_goal": "BUYER_VIEW_CART",
            "transaction_payload": {"__reset__": True},
            "working_memory": clear_active_goal(state),
            "active_form": None,
            "ag_ui_component": None,
        }

    # --- ADD MORE ---
    if str(resolved_id).upper() == "PREORDER_ADD_MORE":
        logger.info("create_preorder: add more requested — back to CART")
        return {
            "status": "WAITING_INPUT",
            "expected_input": "PRODUCT",
            "response_strategy": "ASK_MISSING_FIELD",
            "missing_fields": ["product"],
            "last_missing_field": "product",
            "preorder_workflow": {"phase": "CART"},
            "current_goal": "BUYER_ADD_TO_CART",
            "transaction_payload": {"__reset__": True},
            "ag_ui_component": None,
        }

    # --- PHASE 1: CART → PREORDER_DRAFTED ---
    if phase == "CART":
        if not cart:
            return {
                "status": "WAITING_INPUT",
                "expected_input": "PRODUCT",
                "response_strategy": "ASK_MISSING_FIELD",
                "missing_fields": ["product"],
                "last_missing_field": "product",
                "preorder_workflow": {"phase": "CART"},
                "current_goal": "BUYER_ADD_TO_CART",
                "ag_ui_component": None,
            }

        items_payload = [
            {
                "product_id": item.get("product_id"),
                "name": item.get("name"),
                "quantity": item.get("quantity"),
                "unit": item.get("unit"),
                "price": item.get("price"),
                "producer_id": item.get("producer_id"),
            }
            for item in cart
            if item.get("status") != "DRAFT"
        ]
        meta = CartDomainService.recompute_cart_meta(cart)

        draft_res = await PreorderGateway(mc_runtime).create_draft(
            buyer_phone=phone,
            cart_items=items_payload,
            payment_method=state.get("preferred_payment_method") or "CASH",
            delivery_zone_id=(state.get("buyer_profile") or {}).get("zone_id"),
        )

        preorder_id = draft_res.get("preorder_id") or draft_res.get("id") or "DRAFT"
        if not is_success_response(draft_res) and str(draft_res.get("status") or "").lower() == "error":
            return {
                "status": "ERROR",
                "response_strategy": "ERROR",
                "final_response": draft_res.get("message") or "Impossible de créer le brouillon de précommande.",
                "preorder_workflow": {"phase": "CART"},
                "ag_ui_component": None,
            }

        recap = build_preflight_recap(cart, meta)
        po_menu = preorder_action_menu(str(preorder_id))
        return {
            "status": "WAITING_INPUT",
            "expected_input": "SELECTION",
            "response_strategy": "SELECTION_MENU",
            "final_response": recap,
            "preorder_workflow": {
                "phase": "PREORDER_DRAFTED",
                "preorder_id": str(preorder_id),
                "total_amount": meta.get("total_amount"),
                "items_count": len(items_payload),
            },
            "transaction_payload": {"resolved_id": None},
            "current_goal": "BUYER_PREORDER_INIT",
            "ag_ui_component": None,
            "pending_menu": po_menu,
            "active_cart": cart,
        }

    # --- PHASE 2: PREORDER_DRAFTED → CONFIRMED ---
    if phase == "PREORDER_DRAFTED":
        preorder_id = preorder_flow.get("preorder_id")

        if str(resolved_id).upper() != "PREORDER_CONFIRM":
            # Re-show recap
            meta = CartDomainService.recompute_cart_meta(cart)
            recap = build_preflight_recap(cart, meta)
            po_menu = preorder_action_menu(str(preorder_id or "DRAFT"))
            return {
                "status": "WAITING_INPUT",
                "expected_input": "SELECTION",
                "response_strategy": "SELECTION_MENU",
                "final_response": recap,
                "transaction_payload": {"resolved_id": None},
                "current_goal": "BUYER_PREORDER_INIT",
                "ag_ui_component": None,
                "pending_menu": po_menu,
                "active_cart": cart,
            }

        # Confirm via MCP
        confirm_res = await PreorderGateway(mc_runtime).confirm_draft(
            buyer_phone=phone,
            preorder_id=str(preorder_id),
        )

        if not is_success_response(confirm_res) and str(confirm_res.get("status") or "").lower() == "error":
            return {
                "status": "ERROR",
                "response_strategy": "ERROR",
                "final_response": confirm_res.get("message") or "Impossible de confirmer la précommande.",
                "preorder_workflow": {"phase": "PREORDER_DRAFTED", "preorder_id": preorder_id},
                "ag_ui_component": None,
            }

        order_id = confirm_res.get("order_id") or confirm_res.get("id") or preorder_id
        order_number = confirm_res.get("order_number") or f"#CMD-{str(order_id)[:8]}"
        meta = CartDomainService.recompute_cart_meta(cart)

        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": (
                f"✅ *Précommande confirmée !*\n\n"
                f"📦 Référence : *{order_number}*\n"
                f"💰 Total : {meta.get('total_amount')} {meta.get('currency')}\n"
                f"📋 {len(cart)} article(s)\n\n"
                f"Les producteurs concernés ont été notifiés. "
                f"Vous recevrez une confirmation de disponibilité sous peu.\n\n"
                f"_Tapez *mes commandes* pour suivre votre commande._"
            ),
            "preorder_workflow": {"phase": "CONFIRMED", "preorder_id": str(order_id), "order_number": order_number},
            "last_order_summary": {
                "order_id": str(order_id),
                "order_number": order_number,
                "total_amount": meta.get("total_amount"),
                "currency": meta.get("currency"),
                "items": cart,
            },
            "current_goal": None,
            "goal_status": "COMPLETED",
            "active_form": None,
            "active_cart": [],
            "transaction_payload": {"__reset__": True},
            "draft_payload": {"__reset__": True},
            "working_memory": clear_active_goal(state, clear_cart_snapshot=True),
            "ag_ui_component": None,
        }

    # --- PHASE 3: Already CONFIRMED ---
    if phase == "CONFIRMED":
        order_number = preorder_flow.get("order_number") or "votre commande"
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": f"Votre précommande *{order_number}* est déjà confirmée. Tapez *mes commandes* pour le suivi.",
            "ag_ui_component": None,
        }

    # Fallback
    return {"status": "PLANNING", "ag_ui_component": None}


__all__ = [
    "build_preflight_recap",
    "update_preorder_phase",
    "create_preorder",
]
