"""Market — Buyer Flow (orchestrateur mince).

Côté ACHETEUR du marché AgriConnect. Ce module est un orchestrateur mince
qui délègue chaque domaine à son sous-module spécialisé :

  - ``helpers``      : extraction d'entités, constantes, utilitaires de menu.
  - ``procurement``  : recherche catalogue → escalade appel d'offres.
  - ``cart``         : gestion du panier (ajout, vue, sélection vendeur).
  - ``preorder``     : workflow précommande (draft → recap → confirm).
  - ``negotiation``  : négociation de prix (contre-offre, bids, abandon).
  - ``order_tracking`` : suivi des commandes confirmées.

Re-exporte les symboles publics pour compatibilité ascendante.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agriconnect.graphs.agents.market_coach.services.domain.buyer_common import (
    with_support_footer,
)
from agriconnect.graphs.agents.market_coach.services.domain.cart_service import (
    CartDomainService,
)
from agriconnect.graphs.agents.market_coach.utils import MarketRuntime

from .cart import cart_management
from .helpers import (
    AUCTION_TRACKING_GOALS,
    CART_GOALS,
    NEGOTIATION_GOALS,
    ORDER_TRACKING_GOALS,
    PREORDER_GOALS,
    clear_active_goal,
    draft_block_response,
    draft_requires_completion,
    logger,
    phone_missing_error,
    read_only_intent,
    resolve_product,
)
from .negotiation import negotiation_gate
from .preorder import (
    build_preflight_recap,
    create_preorder,
    update_preorder_phase,
)
from .procurement import (
    build_procurement_escalation,
    buyer_request_resolver,
    resolve_buyer_bid_pick,
    resolve_received_bids,
)

# =====================================================================
# Backward-compat aliases (old private names → new public names)
# =====================================================================
_build_procurement_escalation = build_procurement_escalation
_resolve_received_bids = resolve_received_bids
_resolve_buyer_bid_pick = resolve_buyer_bid_pick
_create_preorder = create_preorder
_build_preflight_recap = build_preflight_recap
_clear_active_goal = clear_active_goal
_update_preorder_phase = update_preorder_phase


def build_product_selection_menu(
    product_name: str,
    vendors: list[dict[str, Any]],
    *,
    extra_context: Optional[dict[str, Any]] = None,
    post_hint: Optional[str] = None,
):
    """Backward-compat wrapper around CartDomainService.build_product_selection_menu.

    The original flow exposed this as a module-level function. We keep that API
    by instantiating a service (mc_runtime not required for this method).
    """
    svc = CartDomainService(mc_runtime=None)  # type: ignore[arg-type]
    return svc.build_product_selection_menu(
        product_name,
        vendors,
        extra_context=extra_context,
        post_hint=post_hint,
    )


# =====================================================================
# NODE 7 — BUYER CONTEXT RESOLVER (orchestrateur de phases)
# =====================================================================


async def buyer_context_resolver(
    state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Dict[str, Any]:
    """Deterministic phase-based orchestrator for the buyer flow.

    Routes to the correct sub-node based on ``current_goal`` and
    ``preorder_workflow["phase"]``. No LLM heuristics, no goal mutations
    (those belong to the goal_planner).
    """
    goal = (state.get("current_goal") or "").upper()
    payload: Dict[str, Any] = state.get("transaction_payload") or {}
    phone = state.get("user_phone")
    preorder_flow: Dict[str, Any] = dict(state.get("preorder_workflow") or {})
    phase = str(preorder_flow.get("phase") or "CART").upper().strip()
    working_memory = state.get("working_memory") or {}
    last_cart_snapshot = working_memory.get("last_active_cart")
    active_cart = state.get("active_cart") or []

    if not active_cart and last_cart_snapshot:
        logger.info(
            "buyer_context_resolver: repopulating active_cart from working_memory snapshot (items=%d)",
            len(last_cart_snapshot),
        )
        state = dict(state)
        state["active_cart"] = list(last_cart_snapshot)
        active_cart = state["active_cart"]

    detected_intent = str(state.get("detected_intent") or "").upper().strip()
    interpreted_event = str(state.get("interpreted_event") or "").upper().strip()
    stable_entities = state.get("stable_entities") or {}

    logger.info(
        "buyer_context_resolver: goal=%s phase=%s intent=%s event=%s cart_items=%d",
        goal,
        phase,
        detected_intent or "",
        interpreted_event or "",
        len(active_cart),
    )

    # ── Finalize helper ──────────────────────────────────────────────
    def _finalize(updates: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(updates or {})
        if "preorder_workflow" not in merged:
            merged["preorder_workflow"] = preorder_flow
        if (
            str(merged.get("status") or "").upper() == "ERROR"
            or str(merged.get("response_strategy") or "").upper() == "ERROR"
        ):
            merged["final_response"] = with_support_footer(merged.get("final_response"))
        return merged

    # ── Guards ────────────────────────────────────────────────────────
    if not phone:
        return _finalize(phone_missing_error())

    # ── Quantity bridge: SEARCH → ADD_TO_CART ─────────────────────────
    qty_candidate = payload.get("quantity")
    if (
        goal not in CART_GOALS
        and phase == "CART"
        and qty_candidate not in (None, "", 0)
        and detected_intent == "BUYER_ADD_TO_CART"
    ):
        product_candidate = _resolve_product_candidate(payload, stable_entities, state)
        if product_candidate:
            logger.info(
                "buyer_context_resolver: bridging to BUYER_ADD_TO_CART (product=%s)",
                product_candidate,
            )
            synthetic = dict(state)
            syn_payload = dict(payload)
            syn_payload.setdefault("product", product_candidate)
            synthetic["transaction_payload"] = syn_payload
            synthetic["current_goal"] = "BUYER_ADD_TO_CART"
            return _finalize(await cart_management(synthetic, mc_runtime))

    # ── Natural confirm / menu reply (PREORDER_DRAFTED action menu) ───
    # `create_preorder` (preorder.py) sait déjà résoudre resolved_id à partir
    # de selection_index (1→CONFIRM, 2→CANCEL, 3→ADD_MORE) OU d'un accord
    # libre — mais rien ne l'appelait pour un événement SELECTION : seul
    # interpreted_event=="CONFIRM" déclenchait ce bloc. Une réponse chiffrée
    # ("1") au menu à 3 options n'était donc JAMAIS transmise à create_preorder
    # et retombait sur la logique panier générique ("quel produit veux-tu
    # ajouter ?"), même pour un choix explicite et sans ambiguïté. On élargit
    # donc le déclencheur à SELECTION également, sans présupposer la réponse :
    # on laisse `create_preorder` résoudre lui-même selection_index/resolved_id.
    if (
        phase == "PREORDER_DRAFTED"
        and preorder_flow.get("preorder_id")
        and not draft_requires_completion(state.get("draft_payload"))
        and (
            goal == "BUYER_PREORDER_CONFIRM"
            or detected_intent == "CONFIRMATION_EXPLICITE"
            or interpreted_event in {"CONFIRM", "SELECTION"}
            or bool(state.get("location_shared"))
        )
    ):
        logger.info(
            "buyer_context_resolver: routing preorder menu reply to create_preorder (event=%s)",
            interpreted_event,
        )
        next_state = dict(state)
        next_payload = dict(next_state.get("transaction_payload") or {})
        # Un CONFIRM en langage libre (sans passer par le menu numéroté) n'a
        # pas de selection_index — on force alors directement l'action ;
        # une SELECTION garde son selection_index/selected_value, résolu par
        # create_preorder lui-même (preorder_choice_from_index).
        if interpreted_event == "CONFIRM" and not next_payload.get("selection_index"):
            next_payload["resolved_id"] = "PREORDER_CONFIRM"
            next_payload.pop("selection_index", None)
            next_payload.pop("selected_value", None)
        next_state["transaction_payload"] = next_payload
        return _finalize(await create_preorder(next_state, mc_runtime))

    # ── Forced preorder phase transitions ─────────────────────────────
    forced = await update_preorder_phase(state, mc_runtime)
    if forced is not None:
        return _finalize(forced)

    # ── Phase-locked routing: if PREORDER_DRAFTED, stay in preorder ───
    if phase == "PREORDER_DRAFTED" and goal not in PREORDER_GOALS:
        logger.info(
            "buyer_context_resolver: forcing preorder branch (phase=%s goal=%s)",
            phase,
            goal,
        )
        goal = "BUYER_PREORDER_INIT"
        state = dict(state)
        state["current_goal"] = goal

    # ── Vendor selection context: force cart routing ────────────────────
    # Fires when the user is mid-vendor-selection or has already picked a
    # vendor and is now supplying the quantity. The presence of vendor_ctx
    # (with either a selection_index or a chosen_vendor) means the buyer
    # is inside the cart tunnel — never re-route to buyer_request_resolver.
    vendor_ctx = state.get("vendor_selection_context")
    logger.info(
        "buyer_context_resolver: vendor_ctx=%s, goal=%s, payload_selection=%s",
        bool(vendor_ctx) if vendor_ctx else None,
        goal,
        payload.get("selection_index"),
    )
    vendor_ctx_valid = (
        vendor_ctx
        and not (isinstance(vendor_ctx, dict) and vendor_ctx.get("__reset__"))
        and goal not in PREORDER_GOALS
        and goal not in NEGOTIATION_GOALS
    )
    if vendor_ctx_valid and isinstance(vendor_ctx, dict):
        ctx_product = str(vendor_ctx.get("product") or "").lower().strip()
        new_product = str(payload.get("product") or "").lower().strip()
        if new_product and ctx_product and new_product != ctx_product:
            logger.info(
                "buyer_context_resolver: stale vendor_ctx (product=%s) vs payload (product=%s) — skipping",
                ctx_product,
                new_product,
            )
            vendor_ctx_valid = False
    if vendor_ctx_valid:
        has_selection = payload.get("selection_index") is not None
        has_chosen = (
            bool(vendor_ctx.get("chosen_vendor"))
            if isinstance(vendor_ctx, dict)
            else False
        )
        if has_selection or has_chosen:
            logger.info(
                "buyer_context_resolver: vendor_ctx active (selection=%s chosen=%s) — forcing cart",
                has_selection,
                has_chosen,
            )
            synthetic = dict(state)
            synthetic["current_goal"] = "BUYER_ADD_TO_CART"
            return _finalize(await cart_management(synthetic, mc_runtime))

    # ── Goal-based routing ────────────────────────────────────────────
    if goal in CART_GOALS:
        return _finalize(await cart_management(state, mc_runtime))

    if goal in PREORDER_GOALS:
        draft = state.get("draft_payload")
        if goal == "BUYER_PREORDER_CONFIRM" and draft_requires_completion(draft):
            return _finalize(draft_block_response(draft))
        return _finalize(await create_preorder(state, mc_runtime))

    if goal in NEGOTIATION_GOALS:
        return _finalize(await negotiation_gate(state, mc_runtime))

    if goal in {"BUYER_REQUEST", "SEARCH_PRODUCTS"}:
        return _finalize(await buyer_request_resolver(state, mc_runtime))

    if goal in ORDER_TRACKING_GOALS:
        from agriconnect.graphs.agents.market_coach.flows.buyer.order_tracking import (
            order_tracking_resolver,
        )

        draft = state.get("draft_payload")
        if draft_requires_completion(draft) and not read_only_intent(goal):
            return _finalize(draft_block_response(draft))
        return _finalize(await order_tracking_resolver(state, mc_runtime))

    if goal in AUCTION_TRACKING_GOALS:
        from agriconnect.graphs.agents.market_coach.flows.buyer.order_tracking import (
            order_tracking_resolver,
        )

        return _finalize(await order_tracking_resolver(state, mc_runtime))

    # ── Auction/bid flows ─────────────────────────────────────────────
    if goal == "MARKET_GET_REQUEST_DETAIL":
        return _finalize(await resolve_received_bids(mc_runtime, str(phone), payload))

    if goal in {
        "PROCUREMENT_ACCEPT_OFFER",
        "PROCUREMENT_SELECT_WINNER",
    } and not payload.get("bid_id"):
        return _finalize(await resolve_buyer_bid_pick(mc_runtime, str(phone), payload))

    # ── Default: planning or draft gate ───────────────────────────────
    draft = state.get("draft_payload")
    if draft_requires_completion(draft) and not read_only_intent(goal):
        return _finalize(draft_block_response(draft))
    return _finalize({"status": "PLANNING", "ag_ui_component": None})


# =====================================================================
# INTERNAL HELPER
# =====================================================================


def _resolve_product_candidate(
    payload: Dict[str, Any],
    stable_entities: Dict[str, Any],
    state: Dict[str, Any],
) -> Optional[str]:
    """Resolve a product name from available sources for bridging."""
    product = resolve_product(payload, stable_entities, state)
    if product:
        return product
    active_cart = state.get("active_cart") or []
    if active_cart:
        last = active_cart[-1]
        if last.get("name"):
            return str(last["name"])
    return None


# =====================================================================
# PUBLIC API — backward-compat re-exports
# =====================================================================

__all__ = [
    # Orchestrator
    "buyer_context_resolver",
    # Sub-modules (re-exported)
    "cart_management",
    "negotiation_gate",
    "buyer_request_resolver",
    "create_preorder",
    "build_preflight_recap",
    "resolve_received_bids",
    "resolve_buyer_bid_pick",
    "build_procurement_escalation",
    # Backward-compat aliases
    "_create_preorder",
    "_resolve_received_bids",
    "_resolve_buyer_bid_pick",
    "_build_procurement_escalation",
    "_build_preflight_recap",
    "build_product_selection_menu",
]
