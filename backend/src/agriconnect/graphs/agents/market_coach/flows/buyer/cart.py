"""Buyer cart management — add to cart, view cart, vendor selection."""

from __future__ import annotations

from typing import Any, Dict, List

from agriconnect.graphs.agents.market_coach.services.domain.cart_service import (
    CartDomainService,
    ProductLookupUnavailable,
)
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    llm_deviation_reply,
)

from .helpers import (
    additional_products_hint,
    capture_cart_draft,
    detect_cart_action,
    logger,
    resolve_product,
    resolve_quantity,
)
from .preorder import create_preorder

# =====================================================================
# CART MANAGEMENT NODE
# =====================================================================


async def cart_management(
    state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Dict[str, Any]:
    """Cart management node — handles add-to-cart, view, and vendor selection.

    Deterministic routing based on goal and available entities:
    - BUYER_VIEW_CART → render cart
    - BUYER_ADD_TO_CART → resolve product, check vendors, add line
    - Free-text preorder keyword → delegate to preorder workflow
    """
    goal = (state.get("current_goal") or "").upper()
    event = str(state.get("interpreted_event") or "").upper().strip()
    user_text = str(state.get("normalized_text") or state.get("user_query") or "")

    payload: Dict[str, Any] = dict(state.get("transaction_payload") or {})
    if not payload and state.get("extracted_entities"):
        payload = dict(state.get("extracted_entities"))

    phone = str(state.get("user_phone") or "")
    cart: List[Dict[str, Any]] = list(state.get("active_cart") or [])
    stable_entities = state.get("stable_entities") or {}
    cart_service = CartDomainService(mc_runtime)

    def _with_base(extra: Dict[str, Any]) -> Dict[str, Any]:
        base: Dict[str, Any] = {"active_cart": cart}
        if goal and "current_goal" not in extra:
            base["current_goal"] = goal
            if "goal_status" not in extra:
                base["goal_status"] = "ACTIVE"
        if "final_response" not in extra:
            base["final_response"] = None
        if "ag_ui_component" not in extra:
            base["ag_ui_component"] = None
        base.update(extra)

        # Persist the last known cart snapshot in working_memory to survive
        # cross-goal transitions (ex: précommande). This is cheap (replace_list)
        # and prevents empty payloads when the planner reroutes via INIT.
        snapshot = base.get("active_cart") or cart
        if snapshot:
            wm_patch = dict(base.get("working_memory") or {})
            wm_patch["last_active_cart"] = snapshot
            base["working_memory"] = wm_patch

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

    # Recover product name from an active vendor context BEFORE the missing-field
    # guards. When the buyer already picked a vendor (or a single vendor was
    # seeded) the product lives in vendor_selection_context, not always in the
    # payload — without this the flow would wrongly re-ask for the product.
    if not product_name:
        _vctx = state.get("vendor_selection_context")
        if isinstance(_vctx, dict) and not _vctx.get("__reset__"):
            _chosen = _vctx.get("chosen_vendor")
            product_name = (
                _chosen.get("name") if isinstance(_chosen, dict) else None
            ) or _vctx.get("product")

    if product_name:
        payload.setdefault("product", product_name)

    quantity = resolve_quantity(payload, stable_entities)

    # Même logique que ci-dessus, pour la quantité : un "oui" confirmant un
    # match incertain (voir needs_confirmation plus bas) n'a pas de quantité
    # dans CE tour — elle a été donnée dans le message précédent et vit dans
    # `requested_quantity`. Sans ce garde-fou, le "Missing product or
    # quantity" ci-dessous redemandait la quantité AVANT même d'atteindre la
    # logique de confirmation, qui ne s'exécute que plus bas. Voir
    # [[buyer-search-fuzzy-match-safety-2026-08]].
    if quantity in (None, "", 0):
        _vctx_qty = state.get("vendor_selection_context")
        if isinstance(_vctx_qty, dict) and not _vctx_qty.get("__reset__"):
            _requested_qty = _vctx_qty.get("requested_quantity")
            if _requested_qty not in (None, "", 0):
                quantity = _requested_qty
                payload["quantity"] = _requested_qty

    # --- VIEW CART ---
    if goal == "BUYER_VIEW_CART":
        meta = CartDomainService.recompute_cart_meta(cart)
        pending_draft = state.get("draft_payload")
        if not pending_draft or pending_draft.get("__reset__"):
            pending_draft = state.get("suspended_payload")
        render = cart_service.render_cart_menu(cart, meta, pending_draft=pending_draft)
        return _with_base(
            {
                "status": "COMPLETED",
                "response_strategy": "SELECTION_MENU" if cart else "SUCCESS",
                "preorder_workflow": {"phase": "CART"},
                "cart_meta": meta,
                **render,
            }
        )

    # --- Guard: insufficient info for non-view goals ---
    if goal not in {"BUYER_ADD_TO_CART", "BUYER_VIEW_CART"} and not (
        product_name and quantity not in (None, "", 0)
    ):
        cart_hint = (
            f" Vous avez {len(cart)} article(s) dans votre panier." if cart else ""
        )
        return _with_base(
            {
                "status": "PLANNING",
                "final_response": (
                    f"🛒 Comment puis-je vous aider ?{cart_hint}\n\n"
                    "💡 _Dites par exemple :_\n"
                    "• *50 kg de maïs* — pour ajouter au panier\n"
                    "• *mon panier* — pour voir votre panier\n"
                    "• *précommander* — pour valider votre commande"
                ),
                "ag_ui_component": None,
            }
        )

    # --- VENDOR SELECTION: check if returning from vendor menu ---
    #
    # Bug réel (2026-08-17) : ce bloc vivait APRÈS le garde "Missing product
    # or quantity" ci-dessous. Tant que la quantité n'était pas encore
    # donnée (le cas NORMAL juste après l'affichage du menu producteurs),
    # ce garde renvoyait TOUJOURS en premier, empêchant selection_index de
    # jamais être résolu en chosen_vendor sur le tour où l'acheteur répond
    # réellement au menu (ou tente d'en choisir un autre). selection_index
    # restait "collant" (voir nodes/memory.py::mapping_kind=="product_vendor")
    # et n'était consommé que plus tard, sur le tour où la quantité arrivait
    # enfin — appliquant alors une sélection potentiellement PÉRIMÉE (un
    # changement d'avis entre-temps écrasait silencieusement le choix
    # précédent, sans jamais être confirmé à l'acheteur). Déplacé AVANT le
    # garde de quantité pour que la sélection/le changement de producteur
    # soit résolu et confirmé sur le MÊME tour où il est demandé — exactement
    # le comportement que le commentaire de memory.py suppose déjà
    # ("cart_management pops it itself once it has successfully located the
    # vendor").
    vendor_ctx = state.get("vendor_selection_context")
    vendor_ctx_active = bool(vendor_ctx) and not (
        isinstance(vendor_ctx, dict) and vendor_ctx.get("__reset__")
    )
    if vendor_ctx_active:
        vendor_ctx_payload: Dict[str, Any] = dict(vendor_ctx or {})
        chosen_vendor = vendor_ctx_payload.get("chosen_vendor")
        previous_vendor = chosen_vendor
        # Robustness: recover product name from the vendor context if it did not
        # survive in the payload (avoids resolve_product_vendors("None")).
        if not product_name:
            product_name = (
                chosen_vendor.get("name") if isinstance(chosen_vendor, dict) else None
            ) or vendor_ctx_payload.get("product")
            if product_name:
                payload["product"] = product_name
        selection_idx = payload.get("selection_index")
        vendor_switched = False

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

                # Un changement RÉEL de producteur (pas la toute première
                # sélection après le menu) doit être confirmé explicitement —
                # sans quoi l'acheteur n'a AUCUN moyen de savoir que sa
                # demande ("je choisis le deuxième") a bien été prise en
                # compte, ni lequel a été retenu (bug réel 2026-08-17).
                if (
                    isinstance(previous_vendor, dict)
                    and isinstance(chosen_vendor, dict)
                    and (
                        previous_vendor.get("producer_id"),
                        previous_vendor.get("product_id"),
                        previous_vendor.get("unit"),
                    )
                    != (
                        chosen_vendor.get("producer_id"),
                        chosen_vendor.get("product_id"),
                        chosen_vendor.get("unit"),
                    )
                ):
                    vendor_switched = True

                requested_qty = vendor_ctx_payload.get("requested_quantity")
                requested_unit = vendor_ctx_payload.get("requested_unit")
                if quantity in (None, "", 0) and requested_qty not in (None, "", 0):
                    payload["quantity"] = requested_qty
                    quantity = requested_qty
                if requested_unit:
                    payload.setdefault("unit", requested_unit)
            else:
                return _with_base(
                    {
                        "status": "WAITING_INPUT",
                        "expected_input": "SELECTION",
                        "response_strategy": "ASK_MISSING_FIELD",
                        "final_response": "Numéro invalide. Choisissez un producteur dans la liste ci-dessus, ou tapez *annuler*.",
                        "ag_ui_component": None,
                    }
                )

        switch_ack = ""
        if vendor_switched and isinstance(chosen_vendor, dict):
            switch_ack = (
                f"🔁 Changement pris en compte : vous avez maintenant choisi "
                f"*{chosen_vendor.get('vendor_name') or 'ce producteur'}* "
                f"(*{chosen_vendor.get('price')} FCFA/{chosen_vendor.get('unit') or 'KG'}*).\n\n"
            )

        # Vendor chosen + quantity available → add to cart directly
        if chosen_vendor is not None and product_name and quantity not in (None, "", 0):
            add_patch = await cart_service.add_to_cart_with_ref(
                phone,
                str(product_name),
                quantity,
                chosen_vendor,
                cart,
                state,
                buyer_unit=payload.get("unit"),
            )
            if switch_ack and add_patch.get("final_response"):
                add_patch["final_response"] = switch_ack + str(add_patch["final_response"])
            return _with_base(add_patch)

        # Vendor chosen but no quantity → ask for it, keep vendor context
        if chosen_vendor is not None and product_name and quantity in (None, "", 0):
            vendor_label = chosen_vendor.get("vendor_name") or "ce producteur"
            unit_hint = chosen_vendor.get("unit") or "KG"
            price_hint = chosen_vendor.get("price")
            price_info = (
                f" (prix : {price_hint} FCFA/{unit_hint})" if price_hint else ""
            )
            n_vendors = len(vendor_ctx_payload.get("vendors") or [])
            switch_hint = (
                f"🔁 _Pour changer de producteur, répondez avec le numéro correspondant "
                f"(1 à {n_vendors} dans la liste ci-dessus)._\n"
                if n_vendors > 1
                else ""
            )
            base_question = (
                f"{switch_ack}"
                f"👤 Vous avez choisi *{vendor_label}* pour *{product_name}*{price_info}.\n\n"
                f"📦 Quelle quantité souhaitez-vous ?\n"
                f"💡 _Exemples : 50 {unit_hint.lower()}, 2 sacs, 100 kg..._\n"
                f"{switch_hint}"
            )
            # Chantier résilience 2026-08 (volet acheteur) : c'est LE point
            # le plus souvent atteint en pratique (vendor_selection_context
            # persiste d'un tour à l'autre) — même correctif que le miroir
            # "Single vendor" plus bas dans ce fichier : final_response
            # précalculé court-circuite l'adaptivité générique de ask.py,
            # et ce chemin est réatteint sur une VRAIE déviation (vendeur
            # déjà choisi, quantité toujours absente).
            note = None
            if event in {"UNKNOWN", "OUT_OF_SCOPE"} and user_text.strip() and not vendor_switched:
                note = await llm_deviation_reply(
                    mc_runtime, user_text, f"répondre à : quelle quantité de {product_name} souhaitez-vous ?",
                )
            return _with_base(
                {
                    "status": "WAITING_INPUT",
                    "expected_input": "QUANTITY",
                    "response_strategy": "ASK_MISSING_FIELD",
                    "final_response": f"{note}\n\n{base_question}" if note else base_question,
                    "transaction_payload": payload,
                    "vendor_selection_context": vendor_ctx_payload,
                    "ag_ui_component": None,
                }
            )

    # --- Missing product or quantity ---
    if not product_name or quantity in (None, "", 0):
        draft_snapshot = capture_cart_draft(state, payload)

        if product_name or quantity not in (None, "", 0):
            draft_item = {
                "product_id": "DRAFT",
                "name": str(product_name) if product_name else "EN ATTENTE",
                "quantity": float(quantity) if quantity not in (None, "", 0) else 0.0,
                "unit": str(payload.get("unit") or "KG"),
                "price": 0.0,
                "line_total": 0.0,
                "status": "DRAFT",
            }
            cart = [
                c
                for c in cart
                if c.get("status") != "DRAFT" and c.get("product_id") != "DRAFT"
            ]
            cart.append(draft_item)

        missing_field_name = "product" if not product_name else "quantity"
        expected = "PRODUCT" if not product_name else "QUANTITY"

        vendor_ctx = state.get("vendor_selection_context")
        return _with_base(
            {
                "status": "WAITING_INPUT",
                "expected_input": expected,
                "response_strategy": "ASK_MISSING_FIELD",
                "missing_fields": [missing_field_name],
                "last_missing_field": missing_field_name,
                "transaction_payload": payload,
                "draft_payload": draft_snapshot
                or state.get("draft_payload")
                or payload,
                "vendor_selection_context": vendor_ctx
                if vendor_ctx
                else state.get("vendor_selection_context"),
                "ag_ui_component": None,
            }
        )

    # --- MULTI-VENDOR RESOLUTION ---
    # Une panne de la recherche catalogue ne doit JAMAIS être rendue comme
    # « produit non disponible » (voir ProductLookupUnavailable) : ça affirme
    # à l'acheteur, à tort, que le produit n'existe pas et l'oriente vers un
    # appel d'offres inutile pour un produit pourtant en stock.
    try:
        vendors, has_multiple = await cart_service.resolve_product_vendors(
            phone, str(product_name)
        )
    except ProductLookupUnavailable:
        return _with_base(
            {
                "status": "ERROR",
                "response_strategy": "ERROR",
                "final_response": (
                    f"🔌 Je n'arrive pas à consulter le catalogue pour « *{product_name}* » "
                    "en ce moment — c'est un souci technique de notre côté, pas une "
                    "absence de stock.\n\nRéessayez dans un instant."
                ),
                "ag_ui_component": None,
            }
        )

    # Un match qui n'est QUE trigram (pas de sous-texte réel entre le terme
    # cherché et le nom trouvé — voir _is_confident_product_match) n'est pas
    # un "peut-être" à faire confirmer : c'est du bruit. Incident réel
    # (2026-08-14) : "oeufs" fuzzy-matchait "Bœuf" alors que ni œufs ni
    # laitue n'existent en base — on écarte ces matches et on retombe sur le
    # flux "produit introuvable" existant plutôt que d'inventer une
    # suggestion. Voir [[buyer-search-fuzzy-match-safety-2026-08]].
    confident_vendors = [v for v in vendors if v.get("match_confident", True)]
    if len(confident_vendors) != len(vendors):
        logger.warning(
            "cart_management: dropped %d low-confidence match(es) for '%s' — treating as not found",
            len(vendors) - len(confident_vendors),
            product_name,
        )
    vendors = confident_vendors
    has_multiple = len(vendors) > 1

    if not vendors:
        return _with_base(
            {
                "status": "COMPLETED",
                "response_strategy": "SUCCESS",
                "final_response": (
                    f"📭 Le produit « *{product_name}* » n'est pas disponible dans notre catalogue actuellement.\n\n"
                    "💡 _Que souhaitez-vous faire ?_\n"
                    "• Tapez un autre nom de produit pour chercher\n"
                    "• Tapez *appel d'offres* pour demander aux producteurs\n"
                    "• Tapez *marketplace* pour voir les produits disponibles"
                ),
                "transaction_payload": {"__reset__": True},
                "negotiation_context": {"__reset__": True},
                "current_goal": None,
                "draft_payload": {"__reset__": True},
                "vendor_selection_context": {"__reset__": True},
                "expected_input": None,
                "ag_ui_component": None,
            }
        )

    if has_multiple:
        extra_context = {
            "requested_quantity": quantity,
            "requested_unit": payload.get("unit"),
        }
        state_patch, _menu = cart_service.build_product_selection_menu(
            str(product_name),
            vendors,
            extra_context=extra_context,
            phone=phone,
        )
        return _with_base(state_patch)

    # Single vendor — add directly if quantity known, else ask
    ref = vendors[0]
    vendor_label = ref.get("vendor_name") or "un producteur"
    unit_hint = ref.get("unit") or "KG"
    price_hint = ref.get("price")
    price_info = f" (prix : {price_hint} FCFA/{unit_hint})" if price_hint else ""
    payload["product"] = product_name
    extras_hint = additional_products_hint(payload, state)

    if quantity in (None, "", 0):
        vendor_ctx_seed = {
            "product": product_name,
            "vendors": [ref],
            "chosen_vendor": ref,
            "requested_quantity": None,
            "requested_unit": None,
            "available_mapping_kind": "product_vendor",
        }
        base_question = (
            f"✅ *{product_name}* est disponible chez *{vendor_label}*{price_info}.\n\n"
            f"📦 Quelle quantité souhaitez-vous ?\n"
            f"💡 _Exemples : 50 {unit_hint.lower()}, 2 sacs, 100 kg..._"
            + extras_hint
        )
        # Chantier résilience 2026-08 (volet acheteur) : ce final_response
        # est précalculé et posé directement sur l'état — comme les tunnels
        # producteur, ça court-circuite le "reuse du final_response
        # précalculé" de render_ask_missing_field (ask.py), qui ne peut donc
        # jamais atteindre SA propre logique d'adaptivité. Ce même code
        # chemin est réatteint sur une VRAIE déviation (vendeur déjà résolu,
        # quantité toujours absente parce que l'utilisateur a dit autre
        # chose) — pas seulement à la première résolution du vendeur — d'où
        # le besoin du même accusé de réception LLM, gardé sur UNKNOWN/
        # OUT_OF_SCOPE pour ne pas payer un appel inutile sur l'entrée
        # fraîche.
        note = None
        if event in {"UNKNOWN", "OUT_OF_SCOPE"} and user_text.strip():
            note = await llm_deviation_reply(
                mc_runtime, user_text, f"répondre à : quelle quantité de {product_name} souhaitez-vous ?",
            )
        return _with_base(
            {
                "status": "WAITING_INPUT",
                "expected_input": "QUANTITY",
                "response_strategy": "ASK_MISSING_FIELD",
                "final_response": f"{note}\n\n{base_question}" if note else base_question,
                "transaction_payload": payload,
                "vendor_selection_context": vendor_ctx_seed,
                "ag_ui_component": None,
            }
        )
    return _with_base(
        await cart_service.add_to_cart_with_ref(
            phone, str(product_name), quantity, ref, cart, state,
            buyer_unit=payload.get("unit"),
        )
    )


__all__ = ["cart_management"]
