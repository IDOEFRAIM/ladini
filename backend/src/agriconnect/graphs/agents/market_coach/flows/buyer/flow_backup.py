"""Market — Buyer Flow (Panier → Sélection Producteur → Précommande → Négociation).

Côté ACHETEUR du marché AgriConnect. Tunnel transactionnel complet :

  1. Recherche produit + menu multi-vendeurs (sélection producteur).
  2. Ajout au panier avec source_type (DIRECT / AUCTION / PROCUREMENT).
  3. Notification MCP aux producteurs intéressés.
  4. Précommande : draft → récapitulatif pre-flight → confirmation.
  5. Négociation de prix (contre-offre, consultation offres, abandon).
  6. Suivi commande (délégué à order_tracking).

Tous les appels MCP filtrent défensivement les ``None``.
"""
from __future__ import annotations

import logging
from datetime import datetime
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from rapidfuzz import fuzz as _fuzz
except ImportError:  # pragma: no cover — graceful degradation
    _fuzz = None  # type: ignore[assignment]

from agriconnect.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)
from agriconnect.graphs.agents.market_coach.services.domain.buyer_common import (
    SUPPORT_FOOTER,
    safe_call_tool,
    with_support_footer,
)
from agriconnect.graphs.agents.market_coach.services.domain.cart_service import (
    CartDomainService,
    SOURCE_TYPE_LABELS,
)
from agriconnect.graphs.agents.market_coach.services.mcp.gateway import AuctionGateway, ProductGateway
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    ensure_dict,
    is_success_response,
)
from agriconnect.graphs.agents.market_coach.flows.buyer.helpers import (
    CART_ACTION_KEYWORDS as _CART_ACTION_KEYWORDS,
    PREORDER_ACTION_OPTIONS as _PREORDER_ACTION_OPTIONS,
    NEGOTIATION_ACTION_OPTIONS as _NEGOTIATION_ACTION_OPTIONS,
    resolve_menu_value_by_index as _resolve_menu_value_by_index,
    preorder_choice_from_index as _preorder_choice_from_index,
    negotiation_choice_from_index as _negotiation_choice_from_index,
    render_interactive_menu,
    preorder_action_menu as _preorder_action_menu,
    negotiation_action_menu as _negotiation_action_menu,
    detect_cart_action as _detect_cart_action,
)

logger = logging.getLogger("AgriConnect.Market.BuyerFlow")


def _build_procurement_escalation(
    payload: Dict[str, Any],
    working_memory: Dict[str, Any],
    product_name: Optional[str],
    unit: Optional[str],
    message: str,
) -> Dict[str, Any]:
    """Construit la réponse d'escalade vers le formulaire AUCTION_CREATE."""
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


def _clear_active_goal(state: Dict[str, Any]) -> Dict[str, Any]:
    working = dict(state.get("working_memory") or {})
    working["active_goal"] = None
    working["locked_intent"] = None
    return working


# =====================================================================
# OWN AUCTIONS — Liste des appels d'offres lancés par l'acheteur
# =====================================================================

async def _resolve_own_auctions(mc_runtime: MarketRuntime, phone: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Récupère les enchères créées par l'acheteur (view_mode='MY_OWN').

    Permet à l'acheteur de naviguer dans ses propres appels d'offres pour
    consulter les offres reçues.
    """
    if not phone:
        return {
            "status": "ERROR",
            "validation_errors": ["missing_user_phone"],
            "response_strategy": "ERROR",
            "final_response": with_support_footer("Numéro de téléphone introuvable, impossible de continuer."),
            "ag_ui_component": None,
        }

    kwargs: Dict[str, Any] = {
        "phone": str(phone),
        "status": "OPEN",
        "view_mode": "MY_OWN",
    }
    product = payload.get("product") or payload.get("product_name")
    if product:
        kwargs["product_name"] = str(product)

    logger.info("_resolve_own_auctions: calling get_auctions with %s", kwargs)
    auction_gw = AuctionGateway(mc_runtime)
    result = await auction_gw.search_open_auctions(**kwargs)
    
    if not is_success_response(result) or int(result.get("count") or 0) == 0:
        msg = result.get("message") or "Vous n'avez aucun appel d'offres ouvert pour l'instant.Si vous pensez que c'est une erreur.Veuillez reessayer"
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": msg,
            "ag_ui_component": None,
        }

    mapping = result.get("mapping") or {}
    menu = result.get("formatted_menu") or "Vos appels d'offres ouverts ont été trouvés."
    
    candidates = [str(d.get("product") ) for d in (result.get("data") or [])]
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


async def buyer_request_resolver(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Recherche d'abord le catalogue puis escalade en appel d'offres sur confirmation explicite."""
    payload: Dict[str, Any] = dict(state.get("transaction_payload") or {})
    if not payload and state.get("extracted_entities"):
        payload = dict(state.get("extracted_entities") or {})

    phone = str(state.get("user_phone") or "")
    if not phone:
        return {
            "status": "ERROR",
            "validation_errors": ["missing_user_phone"],
            "response_strategy": "ERROR",
            "final_response": with_support_footer("Numéro de téléphone introuvable, impossible de continuer."),
            "ag_ui_component": None,
        }

    stable_entities = state.get("stable_entities") or {}
    working_memory = dict(state.get("working_memory") or {})
    normalized_text = str(state.get("normalized_text") or state.get("user_query") or "").strip().lower()
    cart_service = CartDomainService(mc_runtime)

    escalate_keywords = {
        "appel",
        "appels",
        "appel d'offres",
        "appel doffres",
        "appel d offre",
        "lancer appel",
    }
    confirm_keywords = {"oui", "yes", "ok"}
    decline_keywords = {"non", "no", "aucun", "aucune", "annuler"}

    current_goal = str(state.get("current_goal") or "").upper()

    product_name = (
        payload.get("product")
        or payload.get("product_name")
        or stable_entities.get("product")
    )
    if not product_name:
        inferred_product = _infer_product_from_text(state)
        if inferred_product:
            product_name = inferred_product
            payload["product"] = inferred_product

    if not product_name:
        last_product_hint = working_memory.get("buyer_request_last_product")
        if last_product_hint:
            product_name = str(last_product_hint)
            payload.setdefault("product", product_name)

    unit = payload.get("unit_mentioned") or payload.get("unit") or stable_entities.get("unit_mentioned")
    if unit:
        payload.setdefault("unit_mentioned", unit)

    payload.setdefault("product", product_name)

    def _escalate_to_procurement(message: str, unit_hint: Optional[str] = None) -> Dict[str, Any]:
        return _build_procurement_escalation(payload, working_memory, product_name, unit_hint or unit, message)

    selection_idx = payload.get("selection_index")
    if normalized_text in escalate_keywords and product_name:
        return _escalate_to_procurement(
            "Très bien, lançons un appel d'offres. J'aurai besoin du prix minimum, de la quantité souhaitée et d'une date limite.",
            payload.get("unit_mentioned") or payload.get("unit"),
        )

    if current_goal == "PROCUREMENT_CREATE_REQUEST" and state.get("active_form") == "AUCTION_CREATE":
        return _escalate_to_procurement(
            "Très bien, lançons un appel d'offres. J'aurai besoin du prix minimum, de la quantité souhaitée et d'une date limite.",
            payload.get("unit_mentioned") or payload.get("unit"),
        )

    vendor_ctx = state.get("vendor_selection_context")
    if selection_idx is not None and vendor_ctx and not vendor_ctx.get("__reset__"):
        # Recycle existing cart logic une fois le producteur choisi.
        next_state = dict(state)
        next_state["current_goal"] = "BUYER_ADD_TO_CART"
        next_state["transaction_payload"] = dict(payload)
        return await cart_management(next_state, mc_runtime)

    quantity = payload.get("quantity_mentioned")
    if quantity in (None, "", 0):
        quantity = payload.get("quantity")
        if quantity not in (None, "", 0):
            payload["quantity_mentioned"] = quantity
        else:
            quantity = stable_entities.get("quantity_mentioned")
            if quantity not in (None, "", 0):
                payload["quantity_mentioned"] = quantity
                payload["_auto_quantity_fill"] = True

    # --- VENDOR SELECTION TUNNEL ---
    # If a vendor selection menu is active, we must capture the selection even
    # if quantity is still missing; otherwise the user gets asked to re-select.
    vendor_ctx = state.get("vendor_selection_context")
    vendor_ctx_active = bool(vendor_ctx) and not (isinstance(vendor_ctx, dict) and vendor_ctx.get("__reset__"))
    vendor_ctx_payload: Optional[Dict[str, Any]] = dict(vendor_ctx or {}) if vendor_ctx_active else None
    chosen_vendor = (vendor_ctx_payload or {}).get("chosen_vendor") if vendor_ctx_payload else None
    selection_idx = payload.get("selection_index")

    if vendor_ctx_payload is not None and selection_idx is not None:
        vendors_list = vendor_ctx_payload.get("vendors") or []
        try:
            idx = int(selection_idx) - 1
        except (TypeError, ValueError):
            idx = -1
        if 0 <= idx < len(vendors_list):
            chosen_vendor = vendors_list[idx]
            vendor_ctx_payload["chosen_vendor"] = chosen_vendor
            vendor_ctx_payload["chosen_vendor_index"] = int(selection_idx)
            payload.pop("selection_index", None)
            if chosen_vendor:
                payload["product"] = chosen_vendor.get("name") or payload.get("product") or product_name
                product_name = payload.get("product")

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

    # If vendor is already chosen and quantity is available, we can add to cart
    # directly without re-opening the vendor menu.
    if chosen_vendor is not None and product_name and quantity not in (None, "", 0):
        return _with_base(
            await cart_service.add_to_cart_with_ref(
                phone,
                str(product_name),
                quantity,
                chosen_vendor,
                cart,
                state,
            )
        )

    interpreted_event = str(state.get("interpreted_event") or "").upper()
    is_confirm_event = interpreted_event == "CONFIRM"
    is_reject_event = interpreted_event == "REJECT"

    waiting_choice = bool(working_memory.get("buyer_request_waiting_choice"))
    catalog_checked = bool(working_memory.get("buyer_request_catalog_checked"))
    last_product = working_memory.get("buyer_request_last_product") or product_name

    if waiting_choice:
        confirm_signal = normalized_text in confirm_keywords or is_confirm_event
        reject_signal = normalized_text in decline_keywords or is_reject_event

        if confirm_signal or normalized_text in escalate_keywords:
            return _escalate_to_procurement(
                "Très bien, lançons un appel d'offres. J'aurai besoin du prix minimum, de la quantité souhaitée et d'une date limite.",
                unit,
            )
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

    vendors, has_multiple = await cart_service.resolve_product_vendors(phone, str(product_name))

    if vendors:
        extra_context = {
            "requested_quantity": quantity,
            "requested_unit": unit,
        }
        menu_patch, _menu = cart_service.build_product_selection_menu(
            str(product_name),
            vendors,
            extra_context=extra_context,
            post_hint="💡 Si aucune offre ne vous convient, répondez *appel* pour lancer une demande spéciale aux producteurs.",
        )
        menu_patch["transaction_payload"] = payload
        wm = dict(working_memory)
        wm.update(
            {
                "buyer_request_catalog_checked": True,
                "buyer_request_last_product": product_name,
            }
        )
        menu_patch["working_memory"] = wm
        menu_patch["current_goal"] = "BUYER_REQUEST"
        return menu_patch

    wm = dict(working_memory)
    wm.update(
        {
            "buyer_request_catalog_checked": True,
            "buyer_request_waiting_choice": True,
            "buyer_request_last_product": last_product or product_name,
        }
    )
    target_product = last_product or product_name or "ce produit"
    msg = (
        f"Désolé, aucune offre n'est disponible pour « {target_product} » dans notre catalogue. "
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
# RECEIVED BIDS — Offres déposées sur les enchères de l'acheteur
# =====================================================================

async def _resolve_received_bids(mc_runtime: MarketRuntime, phone: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Récupère les bids déposés sur les enchères de l'acheteur."""
    kwargs: Dict[str, Any] = {"status": "OPEN"}
    if not phone:
        return {'error':True,"message":"Numero introuvable"}
    if phone:
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

    # Construction du menu et du mapping côté acheteur (par fournisseur)
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
    candidates = [f"{b.get('producer_name') or b.get('seller_name') or 'Producteur'} ({b.get('product') or b.get('product_name') or 'Produit'})" for b in data]
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
# BID PICK — Sélection d'un bid à accepter / désigner gagnant
# =====================================================================

async def _resolve_buyer_bid_pick(mc_runtime: MarketRuntime, phone: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Résout le `bid_id` requis pour l'acceptation à partir de l'index sélectionné."""
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
            (b for b in data if b and (target in str(b.get("producer_name") ).lower() or target in str(b.get("seller_name") ).lower())),
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

    # Fallback : ré-affiche le catalogue si aucune sélection valide n'est interceptée
    return await _resolve_received_bids(mc_runtime, phone, payload)


# =====================================================================
# TRANSACTIONAL TUNNEL — "Grade Entreprise" (Panier → Précommande → Négociation)
# =====================================================================
#
# Helpers MCP encapsulés (try/except + logs + normalisation) et nodes
# dédiés au tunnel transactionnel acheteur. Le routage entre phases est
# STRICTEMENT déterministe : il dépend uniquement de l'état
# (`preorder_workflow["phase"]` et `current_goal`), jamais d'heuristiques.

# Goals pilotant le tunnel transactionnel.
_CART_GOALS = frozenset({"BUYER_ADD_TO_CART", "BUYER_VIEW_CART"})
_PREORDER_GOALS = frozenset({"BUYER_CREATE_PREORDER", "BUYER_PREORDER_INIT", "BUYER_PREORDER_CONFIRM", "BUYER_CART_RESET"})
_NEGOTIATION_GOALS = frozenset({"BUYER_NEGOTIATE_PRICE"})
_ORDER_TRACKING_GOALS = frozenset({
    "BUYER_CHECK_ORDER_STATUS",
    "BUYER_LIST_ORDERS",
    "BUYER_CANCEL_ORDER",
})

_READ_ONLY_INTENTS = frozenset({
    "BUYER_VIEW_CART",
    "BUYER_LIST_ORDERS",
    "BUYER_CHECK_ORDER_STATUS",
})

_EXPECTED_INPUT_FROM_FIELD = {
    "product": "PRODUCT",
    "quantity_mentioned": "QUANTITY",
    "unit_mentioned": "UNIT",
}

_DRAFT_FIELD_PAIRS = (
    ("product", "product_name"),
    ("quantity_mentioned", "quantity"),
    ("unit_mentioned", "unit"),
)


async def _update_preorder_phase(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any] | None:
    """Force des transitions déterministes du tunnel précommande.

    Objectifs :
    - CART ↔ PREORDER_DRAFTED ↔ CONFIRMED restent synchronisés avec les intents
      envoyés par LangGraph, même si l'utilisateur saute une étape (« confirmer »
      directement après le panier par ex.).
    - Tous les patches retournés écrasent explicitement `preorder_workflow.phase`
      afin de respecter le reducer `merge_dict`.
    """

    goal = str(state.get("current_goal") or "").upper().strip()
    preorder_flow: Dict[str, Any] = dict(state.get("preorder_workflow") or {})
    phase = str(preorder_flow.get("phase") or "CART").upper().strip()
    cart_has_items = bool(state.get("active_cart"))

    def _phase_patch(new_phase: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = {"phase": new_phase}
        if preorder_flow.get("preorder_id"):
            payload["preorder_id"] = preorder_flow.get("preorder_id")
        if extra:
            payload.update(extra)
        return {"preorder_workflow": payload}

    if goal in _CART_GOALS and phase != "CART":
        logger.info("_update_preorder_phase: normalising back to CART phase")
        return _phase_patch("CART")

    if goal == "BUYER_PREORDER_INIT" and phase == "CART":
        logger.info("_update_preorder_phase: INIT from CART -> draft")
        return await _create_preorder(state, mc_runtime)

    if goal == "BUYER_PREORDER_CONFIRM":
        # Utilisateur saute directement la phase brouillon : créer le draft puis confirmer.
        if phase == "CART" and cart_has_items:
            logger.info("_update_preorder_phase: CONFIRM requested while CART — auto-drafting first")
            draft_updates = await _create_preorder(state, mc_runtime)
            draft_flow = dict(draft_updates.get("preorder_workflow") or {})
            draft_phase = str(draft_flow.get("phase") or "").upper()
            # Si la création échoue, on retourne immédiatement le patch (erreurs déjà prêtes).
            if draft_phase != "PREORDER_DRAFTED":
                return draft_updates

            synthetic_state = dict(state)
            synthetic_state["preorder_workflow"] = draft_flow
            synthetic_payload = dict(draft_updates.get("transaction_payload") or state.get("transaction_payload") or {})
            synthetic_payload["resolved_id"] = "PREORDER_CONFIRM"
            synthetic_payload.pop("selection_index", None)
            synthetic_payload.pop("selected_value", None)
            synthetic_state["transaction_payload"] = synthetic_payload
            synthetic_state["active_cart"] = draft_updates.get("active_cart", state.get("active_cart"))

            confirm_updates = await _create_preorder(synthetic_state, mc_runtime)
            if "preorder_workflow" not in confirm_updates:
                confirm_updates["preorder_workflow"] = draft_flow
            return confirm_updates

        if phase == "PREORDER_DRAFTED":
            logger.info("_update_preorder_phase: CONFIRM from PREORDER_DRAFTED -> confirm")
            next_state = dict(state)
            next_payload = dict(next_state.get("transaction_payload") or {})
            next_payload["resolved_id"] = "PREORDER_CONFIRM"
            next_payload.pop("selection_index", None)
            next_payload.pop("selected_value", None)
            next_state["transaction_payload"] = next_payload
            return await _create_preorder(next_state, mc_runtime)

    return None


async def _resolve_product_ref(
    mc_runtime: MarketRuntime,
    phone: str,
    product_name: str,
) -> Optional[Dict[str, Any]]:
    """Résout un nom de produit en référence catalogue (id, prix, unité, vendeur).

    Retourne le meilleur match unique OU ``None`` si aucun produit disponible.
    Pour lister tous les vendeurs disponibles, utiliser ``CartDomainService.resolve_product_vendors``.
    """
    if not product_name:
        return None
    res = await ProductGateway(mc_runtime).search_products(product=str(product_name), phone=str(phone))
    if not is_success_response(res):
        return None
    results = res.get("results") or (res.get("data") or {}).get("results") or []
    if not results:
        return None
    best = results[0]
    source_type = _infer_source_type(best)
    return {
        "product_id": str(best.get("id")),
        "crop_cycle_id": best.get("crop_cycle_id"),
        "name": str(best.get("name") or product_name),
        "price": float(best.get("price") or 0.0),
        "unit": str(best.get("unit") or "KG").upper(),
        "vendor": best.get("vendor"),
        "vendor_name": best.get("vendor_name") or best.get("producer_name") or best.get("vendor"),
        "producer_id": best.get("producer_id") or best.get("vendor_id"),
        "source_type": source_type,
        "is_auction": source_type == "AUCTION",
        "estimated_available_at": best.get("estimated_available_at"),
    }


def _infer_source_type(product_record: Dict[str, Any]) -> str:
    """Détermine la source du produit : DIRECT, FUTURE, AUCTION ou PROCUREMENT."""
    src = str(product_record.get("source_type") or product_record.get("type") or "").upper()
    if src in ("AUCTION", "PROCUREMENT", "FUTURE"):
        return src
    if product_record.get("crop_cycle_id"):
        return "FUTURE"
    if product_record.get("auction_id") or product_record.get("is_auction"):
        return "AUCTION"
    return "DIRECT"

def _missing_draft_field(draft: Optional[Dict[str, Any]]) -> Optional[str]:
    if not draft or draft.get("__reset__"):
        return None
    for primary, fallback in _DRAFT_FIELD_PAIRS:
        value = draft.get(primary)
        if value in (None, "", 0, [], {}):
            alt = draft.get(fallback) if fallback else None
            if alt in (None, "", 0, [], {}):
                return primary
    return None


def _draft_requires_completion(draft: Optional[Dict[str, Any]]) -> bool:
    return _missing_draft_field(draft) is not None


def _read_only_intent(intent: Optional[str]) -> bool:
    return str(intent or "").upper() in _READ_ONLY_INTENTS


def _pending_draft_prompt(draft: Optional[Dict[str, Any]]) -> str:
    summary = CartDomainService.format_pending_draft(draft)
    if summary:
        return summary
    return (
        "✏️ *Ajout en cours*. Indiquez le produit, la quantité et l'unité "
        "avant de confirmer la précommande."
    )


def _draft_expected_input(draft: Optional[Dict[str, Any]]) -> str:
    missing = _missing_draft_field(draft)
    if not missing:
        return "NONE"
    return _EXPECTED_INPUT_FROM_FIELD.get(missing, "NONE")


def _draft_block_response(draft: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "status": "WAITING_INPUT",
        "response_strategy": "ASK_MISSING_FIELD",
        "final_response": _pending_draft_prompt(draft),
        "expected_input": _draft_expected_input(draft),
        "ag_ui_component": None,
    }


_PRODUCT_HINT_PATTERN = re.compile(r"(?:\bde\b|\bdu\b|\bdes\b|d')\s+([a-zàâçéèêëîïôûùüÿñæœ'\-]+(?:\s+[a-zàâçéèêëîïôûùüÿñæœ'\-]+)?)", re.IGNORECASE)
_PRODUCT_STOP_WORDS = {
    "kg", "kgs", "kilo", "kilos", "kilogramme", "kilogrammes", "tonne", "tonnes",
    "sac", "sacs", "panier", "paniers", "de", "du", "des", "d", "le", "la", "les",
    "un", "une", "au", "aux", "en", "pour", "avec", "mon", "ma", "mes", "ton",
    "ta", "tes", "son", "sa", "ses", "et", "ou"
}


def _infer_product_from_text(state: Dict[str, Any]) -> Optional[str]:
    text = str(state.get("normalized_text") or state.get("user_query") or "").strip()
    if not text:
        return None

    hint = _PRODUCT_HINT_PATTERN.search(text)
    if hint:
        candidate = hint.group(1).strip(" .,;!?")
        if candidate and candidate.lower() not in _PRODUCT_STOP_WORDS:
            return candidate

    tokens = re.findall(r"[a-zàâçéèêëîïôûùüÿñæœ'\-]+", text.lower())
    for token in reversed(tokens):
        if token not in _PRODUCT_STOP_WORDS and not token.isdigit():
            return token
    return None


def _capture_cart_draft(state: Dict[str, Any], payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    draft = dict(state.get("draft_payload") or {})
    changed = False
    for key in ("product", "product_name", "quantity", "quantity_mentioned", "unit", "unit_mentioned"):
        value = payload.get(key)
        if value in (None, "", 0, [], {}):
            continue
        if draft.get(key) != value:
            draft[key] = value
            changed = True

    if changed:
        return draft
    if draft and not draft.get("__reset__"):
        return draft
    return None


# =====================================================================
# NODE — CART MANAGEMENT (gestion panier + vérification atomique stock)
# =====================================================================
async def cart_management(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    goal = (state.get("current_goal") or "").upper()

    payload: Dict[str, Any] = dict(state.get("transaction_payload") or {})
    if not payload and state.get("extracted_entities"):
        payload = dict(state.get("extracted_entities"))

    phone = str(state.get("user_phone") or "")
    cart: List[Dict[str, Any]] = list(state.get("active_cart") or [])
    stable_entities = state.get("stable_entities") or {}
    cart_service = CartDomainService(mc_runtime)

    def _with_base(extra: Dict[str, Any]) -> Dict[str, Any]:
        base: Dict[str, Any] = {
            "active_cart": cart,
        }
        # Only set final_response/ag_ui_component to None if caller didn't provide them
        if "final_response" not in extra:
            base["final_response"] = None
        if "ag_ui_component" not in extra:
            base["ag_ui_component"] = None
        base.update(extra)
        return base

    cart_action = _detect_cart_action(state)
    if cart_action == "PREORDER" and cart:
        logger.info("cart_management: preorder command detected via free text")
        next_state = dict(state)
        next_state["current_goal"] = "BUYER_PREORDER_INIT"
        next_state["active_cart"] = cart
        next_state["transaction_payload"] = dict(payload)
        preorder_patch = await _create_preorder(next_state, mc_runtime)
        return _with_base(preorder_patch)

    product_name = payload.get("product") or payload.get("product_name") or stable_entities.get("product")
    if not product_name:
        inferred_product = _infer_product_from_text(state)
        if inferred_product:
            product_name = inferred_product
            payload["product"] = inferred_product
    if product_name:
        payload.setdefault("product", product_name)

    quantity = payload.get("quantity_mentioned")
    if quantity in (None, "", 0):
        quantity = payload.get("quantity") or stable_entities.get("quantity_mentioned")
        if quantity not in (None, "", 0):
            payload["quantity_mentioned"] = quantity

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

    if goal not in {"BUYER_ADD_TO_CART", "BUYER_VIEW_CART"} and not (product_name and quantity not in (None, "", 0)):
        return _with_base({"status": "PLANNING", "final_response": "Comment puis-je vous aider avec votre panier ?", "ag_ui_component": None})

    if not product_name or quantity in (None, "", 0):
        draft_snapshot = _capture_cart_draft(state, payload)
        
        if product_name or quantity not in (None, "", 0):
            draft_item = {
                "product_id": "DRAFT",
                "name": str(product_name) if product_name else "EN ATTENTE",
                "quantity": float(quantity) if quantity not in (None, "", 0) else 0.0,
                "unit": str(payload.get("unit_mentioned") or "KG"),
                "price": 0.0,
                "line_total": 0.0,
                "status": "DRAFT"
            }
            cart = [c for c in cart if c.get("status") != "DRAFT" and c.get("product_id") != "DRAFT"]
            cart.append(draft_item)

        missing_field = "PRODUCT" if not product_name else "QUANTITY"
        prompt = (
            "Quel produit souhaitez-vous ajouter au panier ?"
            if not product_name
            else f"Quelle quantité de {product_name} souhaitez-vous ?"
        )
        return _with_base({
            "status": "WAITING_INPUT",
            "expected_input": missing_field,
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": prompt,
            "transaction_payload": payload,
            "draft_payload": draft_snapshot or state.get("draft_payload") or payload,
            "vendor_selection_context": vendor_ctx_payload if vendor_ctx_payload is not None else state.get("vendor_selection_context"),
            "ag_ui_component": None,
        })

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
        # Present vendor selection menu
        state_patch, _menu = cart_service.build_product_selection_menu(
            str(product_name),
            vendors,
            extra_context=extra_context,
        )
        return _with_base(state_patch)

    # Single vendor — proceed directement via le domaine
    ref = vendors[0]
    return _with_base(
        await cart_service.add_to_cart_with_ref(
            phone,
            str(product_name),
            quantity,
            ref,
            cart,
            state,
        )
    )


# =====================================================================
# PREORDER WORKFLOW — draft / pre-flight recap / confirm
# =====================================================================

def _build_preflight_recap(cart: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    """Construit le récapitulatif pre-flight avant confirmation finale."""
    lines = ["📋 *Récapitulatif de votre précommande :*\n"]
    filtered_items: List[Dict[str, Any]] = []
    for item in cart:
        try:
            qty_val = float(item.get("quantity") or 0.0)
        except (TypeError, ValueError):
            qty_val = 0.0
        if qty_val <= 0:
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
    lines.append("\nFaites un choix dans le menu ci-dessous :")
    lines.append(render_interactive_menu(_PREORDER_ACTION_OPTIONS))
    lines.append("Répondez uniquement avec le numéro correspondant (ex: 1).")
    return "\n".join(lines)


async def _create_preorder(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Workflow de précommande à 3 phases :
    1. CART → PREORDER_DRAFTED : Crée le brouillon MCP + affiche pre-flight recap.
    2. PREORDER_DRAFTED + CONFIRM → CONFIRMED : Confirme la précommande MCP.
    3. Actions secondaires (PREORDER_ADD_MORE, PREORDER_CANCEL).
    """
    goal = str(state.get("current_goal") or "").upper()
    payload: Dict[str, Any] = dict(state.get("transaction_payload") or {})
    phone = str(state.get("user_phone") or "")
    cart: List[Dict[str, Any]] = list(state.get("active_cart") or [])
    preorder_flow: Dict[str, Any] = dict(state.get("preorder_workflow") or {})
    phase = str(preorder_flow.get("phase") or "CART").upper().strip()

    # --- CANCEL NAVIGATION ---
    resolved_id = payload.get("resolved_id")
    if not resolved_id:
        selection_idx = payload.get("selection_index")
        mapped_choice = _preorder_choice_from_index(selection_idx)
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
    if str(resolved_id).upper() == "PREORDER_CANCEL":
        logger.info("_create_preorder: cancel requested — returning to CART")
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": "↩️ Précommande annulée. Votre panier est toujours disponible.",
            "preorder_workflow": {"phase": "CART"},
            "current_goal": "BUYER_VIEW_CART",
            "transaction_payload": {"__reset__": True},
            "working_memory": _clear_active_goal(state),
            "active_form": None,
            "ag_ui_component": None,
        }

    # --- ADD MORE ---
    if str(resolved_id).upper() == "PREORDER_ADD_MORE":
        logger.info("_create_preorder: add more requested — back to CART")
        return {
            "status": "WAITING_INPUT",
            "expected_input": "PRODUCT",
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": "Quel produit souhaitez-vous ajouter au panier ?",
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
                "final_response": "Votre panier est vide. Ajoutez au moins un produit avant de précommander.",
                "preorder_workflow": {"phase": "CART"},
                "current_goal": "BUYER_ADD_TO_CART",
                "ag_ui_component": None,
            }

        # Create draft via MCP
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

        draft_res = await safe_call_tool(
            mc_runtime,
            "create_preorder_draft",
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

        # Pre-flight recap
        recap = _build_preflight_recap(cart, meta)
        preorder_menu = _preorder_action_menu(str(preorder_id))

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
            "pending_menu": preorder_menu,
        }

    # --- PHASE 2: PREORDER_DRAFTED → CONFIRMED ---
    if phase == "PREORDER_DRAFTED":
        preorder_id = preorder_flow.get("preorder_id")

        if str(resolved_id).upper() != "PREORDER_CONFIRM":
            # User hasn't confirmed yet — re-show recap
            meta = CartDomainService.recompute_cart_meta(cart)
            recap = _build_preflight_recap(cart, meta)
            preorder_menu = _preorder_action_menu(str(preorder_id or "DRAFT"))
            return {
                "status": "WAITING_INPUT",
                "expected_input": "SELECTION",
                "response_strategy": "SELECTION_MENU",
                "final_response": recap,
                "transaction_payload": {"resolved_id": None},
                "current_goal": "BUYER_PREORDER_INIT",
                "ag_ui_component": None,
                "pending_menu": preorder_menu,
            }

        # Confirm the preorder via MCP
        confirm_res = await safe_call_tool(
            mc_runtime,
            "confirm_preorder_draft",
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
            "working_memory": _clear_active_goal(state),
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
    return {
        "status": "PLANNING",
        "ag_ui_component": None,
    }


# =====================================================================
# NODE — NEGOTIATION GATE (ouverture / suivi des contre-offres)
# =====================================================================

async def negotiation_gate(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    goal = (state.get("current_goal") or "").upper()
    payload: Dict[str, Any] = state.get("transaction_payload") or {}
    phone = str(state.get("user_phone") or "")
    stable_entities = state.get("stable_entities") or {}

    nctx: Dict[str, Any] = dict(state.get("negotiation_context") or {})
    nphase = str(nctx.get("phase") or "NEGOTIATION_MENU").upper().strip()
    auction_id = nctx.get("auction_id") or nctx.get("session_id")

    product_name = (
        payload.get("product")
        or payload.get("product_name")
        or stable_entities.get("product")
    )

    quantity = payload.get("quantity_mentioned")
    if quantity in (None, "", 0):
        quantity = payload.get("quantity") or stable_entities.get("quantity_mentioned")

    if goal not in _NEGOTIATION_GOALS:
        return {"status": "PLANNING", "ag_ui_component": None}

    if auction_id and nphase == "AWAIT_COUNTER_PRICE":
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
        neg_menu = _negotiation_action_menu(str(auction_id))
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

    if auction_id and nphase == "VIEWING_OFFERS":
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

        bids_res = await safe_call_tool(mc_runtime, "get_auction_bids", auction_id=str(auction_id))
        bids = bids_res.get("bids") or []
        if str(bids_res.get("status") or "").lower() != "success" or not bids:
            msg = bids_res.get("message") or "Aucune offre reçue pour l'instant."
            neg_fb_menu = _negotiation_action_menu(str(auction_id))
            return {
                "status": "WAITING_INPUT",
                "expected_input": "SELECTION",
                "response_strategy": "SELECTION_MENU",
                "final_response": msg,
                "transaction_payload": {"resolved_id": None},
                "negotiation_context": {**nctx, "phase": "NEGOTIATION_MENU"},
                "ag_ui_component": None,
                "pending_menu": neg_fb_menu,
            }

        mapping: Dict[str, str] = {}
        lines = ["📥 *Offres reçues :*"]
        options = []
        for i, b in enumerate(bids, start=1):
            bid_id2 = str(b.get("bid_id") or b.get("id") or "")
            producer = b.get("producer") or b.get("producer_name") or "Producteur"
            price = b.get("price") or b.get("offered_price") or "?"
            lines.append(f"\n*{i}. {producer}* — 💰 {price} CFA")
            options.append({"index": str(i), "label": f"{producer} — {price} CFA"})
            if bid_id2:
                mapping[str(i)] = bid_id2
        lines.append("\n_Répondez avec le numéro pour accepter une offre._")

        bids_text = "\n".join(lines)
        return {
            "status": "WAITING_INPUT",
            "expected_input": "SELECTION",
            "response_strategy": "SELECTION_MENU",
            "final_response": bids_text,
            "transaction_payload": {"resolved_id": None, "bid_id": None},
            "negotiation_context": {**nctx, "phase": "VIEWING_OFFERS"},
            "ag_ui_component": None,
            "pending_menu": MenuRequest(
                title="Offres reçues",
                options=[MenuOption(index=o["index"], label=o["label"], value=mapping.get(o["index"])) for o in options],
                kind="bid",
                metadata={"auction_id": str(auction_id)},
                preformatted_text=bids_text,
            ),
        }

    if auction_id and nphase == "NEGOTIATION_MENU":
        action = payload.get("resolved_id")
        if not action:
            selection_idx = payload.get("selection_index")
            mapped = _negotiation_choice_from_index(selection_idx)
            if mapped:
                action = mapped
                payload["resolved_id"] = action
            if selection_idx not in (None, ""):
                payload.pop("selection_index", None)

        action = (str(action).upper().strip() if action else "")
        if not action:
            msg = nctx.get("last_message") or state.get("final_response") or "Négociation en cours."
            neg_idle_menu = _negotiation_action_menu(str(auction_id))
            return {
                "status": "WAITING_INPUT",
                "expected_input": "SELECTION",
                "response_strategy": "SELECTION_MENU",
                "final_response": msg,
                "ag_ui_component": None,
                "pending_menu": neg_idle_menu,
            }

        if action == "NEGOTIATION_VIEW_OFFERS":
            bids_res = await safe_call_tool(mc_runtime, "get_auction_bids", auction_id=str(auction_id))
            bids = bids_res.get("bids") or []
            if str(bids_res.get("status") or "").lower() != "success" or not bids:
                msg = bids_res.get("message") or "Aucune offre reçue pour l'instant."
                neg_vo_menu = _negotiation_action_menu(str(auction_id))
                return {
                    "status": "WAITING_INPUT",
                    "expected_input": "SELECTION",
                    "response_strategy": "SELECTION_MENU",
                    "final_response": msg,
                    "transaction_payload": {"resolved_id": None},
                    "negotiation_context": {**nctx, "phase": "NEGOTIATION_MENU"},
                    "ag_ui_component": None,
                    "pending_menu": neg_vo_menu,
                }

            mapping: Dict[str, str] = {}
            lines = ["📥 *Offres reçues :*"]
            options = []
            for i, b in enumerate(bids, start=1):
                bid_id2 = str(b.get("bid_id") or b.get("id") or "")
                producer = b.get("producer") or b.get("producer_name") or "Producteur"
                price = b.get("price") or b.get("offered_price") or "?"
                lines.append(f"\n*{i}. {producer}* — 💰 {price} CFA")
                options.append({"index": str(i), "label": f"{producer} — {price} CFA"})
                if bid_id2:
                    mapping[str(i)] = bid_id2
            lines.append("\n_Répondez avec le numéro pour accepter une offre._")

            bids_text2 = "\n".join(lines)
            return {
                "status": "WAITING_INPUT",
                "expected_input": "SELECTION",
                "response_strategy": "SELECTION_MENU",
                "final_response": bids_text2,
                "transaction_payload": {"resolved_id": None, "bid_id": None},
                "negotiation_context": {**nctx, "phase": "VIEWING_OFFERS"},
                "ag_ui_component": None,
                "pending_menu": MenuRequest(
                    title="Offres reçues",
                    options=[MenuOption(index=o["index"], label=o["label"], value=mapping.get(o["index"])) for o in options],
                    kind="bid",
                    metadata={"auction_id": str(auction_id)},
                    preformatted_text=bids_text2,
                ),
            }

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

    if not product_name or (payload.get("price_mentioned") in (None, "", 0)):
        return {
            "status": "WAITING_INPUT",
            "expected_input": "PRODUCT" if not product_name else "PRICE",
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": "Quel produit souhaitez-vous négocier et à quel prix ?",
            "ag_ui_component": None,
        }

    ref = await _resolve_product_ref(mc_runtime, phone, str(product_name))
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
        options=_negotiation_action_menu(str(res.get("negotiation_id") or "")).options,
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


# =====================================================================
# NODE 7 — CONTEXT RESOLVER (BUYER) — ORCHESTRATEUR DE PHASES
# =====================================================================

async def buyer_context_resolver(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Orchestrateur déterministe du flow Acheteur.

    Aiguille vers le bon sous-node UNIQUEMENT en fonction de l'état :
    `current_goal` et `preorder_workflow["phase"]`. Aucune heuristique LLM,
    aucune écriture de goal (réservée au goal_planner).
    """
    goal = (state.get("current_goal") or "").upper()
    payload: Dict[str, Any] = state.get("transaction_payload") or {}
    phone = state.get("user_phone")
    preorder_flow: Dict[str, Any] = dict(state.get("preorder_workflow") or {})
    phase = str(preorder_flow.get("phase") or "CART").upper().strip()
    detected_intent = str(state.get("detected_intent") or "").upper().strip()
    interpreted_event = str(state.get("interpreted_event") or "").upper().strip()
    stable_entities = state.get("stable_entities") or {}

    logger.info(
        "buyer_context_resolver: goal=%s phase=%s intent=%s event=%s cart_items=%d",
        goal,
        phase,
        detected_intent or "",
        interpreted_event or "",
        len(state.get("active_cart") or []),
    )

    def _finalize(updates: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(updates or {})
        if "preorder_workflow" not in merged:
            merged["preorder_workflow"] = preorder_flow
        if (
            str(merged.get("status") or "").upper() == "ERROR"
            or str(merged.get("response_strategy") or "").upper() == "ERROR"
        ):
            merged["final_response"] = with_support_footer(merged.get("final_response"))
        logger.debug("buyer_context_resolver state update: %s", merged)
        try:
            out_phase = (merged.get("preorder_workflow") or {}).get("phase")
        except Exception:
            out_phase = None
        logger.debug(
            "buyer_context_resolver out: goal=%s phase_in=%s phase_out=%s",
            goal,
            phase,
            out_phase,
        )
        return merged

    if not phone:
        return _finalize({
            "status": "ERROR",
            "validation_errors": ["missing_user_phone"],
            "response_strategy": "ERROR",
            "final_response": "Numéro de téléphone introuvable, impossible de continuer.",
            "ag_ui_component": None,
        })

    def _resolve_product_candidate() -> Optional[str]:
        if payload.get("product"):
            return str(payload["product"])
        if payload.get("product_name"):
            return str(payload["product_name"])
        if stable_entities.get("product"):
            return str(stable_entities["product"])
        tx_payload = state.get("transaction_payload") or {}
        if tx_payload.get("product"):
            return str(tx_payload["product"])
        active_cart = state.get("active_cart") or []
        if active_cart:
            last = active_cart[-1]
            if last.get("name"):
                return str(last["name"])
        return None

    # Autorise un enchaînement direct SEARCH -> ADD_TO_CART dès qu'une quantité est fournie.
    qty_candidate = payload.get("quantity_mentioned")
    if (
        goal not in _CART_GOALS
        and phase == "CART"
        and qty_candidate not in (None, "", 0)
        and detected_intent == "BUYER_ADD_TO_CART"
    ):
        bridged_product = _resolve_product_candidate()
        if bridged_product:
            logger.info(
                "buyer_context_resolver: bridging quantity to BUYER_ADD_TO_CART (product=%s)",
                bridged_product,
            )
            synthetic_state = dict(state)
            synthetic_payload = dict(payload)
            synthetic_payload.setdefault("product", bridged_product)
            synthetic_state["transaction_payload"] = synthetic_payload
            synthetic_state["current_goal"] = "BUYER_ADD_TO_CART"
            return _finalize(await cart_management(synthetic_state, mc_runtime))

    natural_confirm = (
        phase == "PREORDER_DRAFTED"
        and preorder_flow.get("preorder_id")
        and not _draft_requires_completion(state.get("draft_payload"))
        and (
            goal == "BUYER_PREORDER_CONFIRM"
            or detected_intent == "CONFIRMATION_EXPLICITE"
            or interpreted_event == "CONFIRM"
        )
    )
    if natural_confirm:
        logger.info(
            "buyer_context_resolver: auto-confirm triggered (goal=%s intent=%s event=%s)",
            goal,
            detected_intent or "<none>",
            interpreted_event or "<none>",
        )
        next_state = dict(state)
        next_payload = dict(next_state.get("transaction_payload") or {})
        next_payload["resolved_id"] = "PREORDER_CONFIRM"
        next_payload.pop("selection_index", None)
        next_payload.pop("selected_value", None)
        next_state["transaction_payload"] = next_payload
        auto_confirm = await _create_preorder(next_state, mc_runtime)
        return _finalize(auto_confirm)

    forced = await _update_preorder_phase(state, mc_runtime)
    if forced is not None:
        return _finalize(forced)

    # 1. Tunnel transactionnel — routage par goal (phase pilotée dans les nodes)
    if phase == "PREORDER_DRAFTED" and goal not in _PREORDER_GOALS:
        logger.info(
            "buyer_context_resolver: forcing preorder branch (phase=%s goal=%s)",
            phase,
            goal,
        )
        goal = "BUYER_PREORDER_INIT"
        state = dict(state)
        state["current_goal"] = goal
    if goal in _CART_GOALS:
        return _finalize(await cart_management(state, mc_runtime))
    if goal in _PREORDER_GOALS:
        draft = state.get("draft_payload")
        if goal == "BUYER_PREORDER_CONFIRM" and _draft_requires_completion(draft):
            return _finalize(_draft_block_response(draft))
        return _finalize(await _create_preorder(state, mc_runtime))
    if goal in _NEGOTIATION_GOALS:
        return _finalize(await negotiation_gate(state, mc_runtime))
    if goal == "BUYER_REQUEST":
        return _finalize(await buyer_request_resolver(state, mc_runtime))
    if goal == "MARKET_GET_REQUESTS":
        return _finalize(await _resolve_own_auctions(mc_runtime, str(phone), payload))
    if goal in _ORDER_TRACKING_GOALS:
        from agriconnect.graphs.agents.market_coach.flows.buyer.order_tracking import (
            order_tracking_resolver,
        )
        draft = state.get("draft_payload")
        if _draft_requires_completion(draft) and not _read_only_intent(goal):
            return _finalize(_draft_block_response(draft))
        return _finalize(await order_tracking_resolver(state, mc_runtime))

    # 2. Flows enchères/bids existants
    if goal == "MARKET_GET_REQUEST_DETAIL":
        return _finalize(await _resolve_received_bids(mc_runtime, str(phone), payload))
    if goal in {"PROCUREMENT_ACCEPT_OFFER", "PROCUREMENT_SELECT_WINNER"} and not payload.get("bid_id"):
        return _finalize(await _resolve_buyer_bid_pick(mc_runtime, str(phone), payload))

    # 3. Par défaut, bascule sur la planification si aucun ID n'est manquant
    draft = state.get("draft_payload")
    if _draft_requires_completion(draft) and not _read_only_intent(goal):
        return _finalize(_draft_block_response(draft))
    return _finalize({"status": "PLANNING", "ag_ui_component": None})


__all__ = [
    "_resolve_own_auctions",
    "_resolve_received_bids",
    "_resolve_buyer_bid_pick",
    "buyer_request_resolver",
    "buyer_context_resolver",
    # Tunnel transactionnel "Grade Entreprise"
    "safe_call_tool",
    "cart_management",
    "negotiation_gate",
    "_create_preorder",
    "_add_to_cart_with_ref",
    "build_product_selection_menu",
    "_resolve_product_vendors",
]