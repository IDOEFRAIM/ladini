"""Buyer procurement — auction creation escalation + own-auctions listing."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    clear_pending_interaction,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)
from ladini.graphs.agents.market_coach.services.mcp.gateway import (
    AuctionGateway,
    ModerationGateway,
)
from ladini.graphs.agents.market_coach.utils import (
    MarketRuntime,
    is_success_response,
)

from .cart import cart_management
from .helpers import (
    ESCALATE_KEYWORDS,
    additional_products_hint,
    logger,
    phone_missing_error,
    resolve_product,
    resolve_quantity,
    resolve_unit,
)


def _fmt_num(value: Any) -> str:
    """Format a numeric value without a trailing ``.0`` (225.0 → '225')."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f == int(f) else str(f)


# =====================================================================
# PROCUREMENT ESCALATION BUILDER
# =====================================================================


def build_procurement_escalation(
    payload: Dict[str, Any],
    working_memory: Dict[str, Any],
    product_name: Optional[str],
    unit: Optional[str],
    message: str,
    existing_form_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build state patch that escalates to the AUCTION_CREATE form.

    `existing_form_data` : progrès déjà collecté par `form_node`/`run_form_step`
    (ex: prix et quantité déjà répondus) quand cette fonction est appelée pour
    RÉ-ENTRER dans un formulaire AUCTION_CREATE déjà actif, plutôt que pour en
    démarrer un nouveau. Sans ça, `buyer_request_resolver` (qui ré-escalade à
    chaque tour tant que `current_goal==PROCUREMENT_CREATE_REQUEST` et
    `active_form==AUCTION_CREATE`, voir l'appelant) reconstruisait `form_data`
    quasiment à vide à chaque fois, effaçant prix/quantité déjà fournis et
    faisant paraître "date limite" comme la seule info manquante en boucle.
    """
    next_payload = dict(payload)
    if product_name:
        next_payload.setdefault("product", product_name)
    # None-overwrite (merge_dict) : un pop sur le patch retourné ne supprime
    # rien — une sélection périmée pouvait être ré-appliquée au tour suivant.
    next_payload["selection_index"] = None
    next_payload["selected_value"] = None
    if next_payload.pop("_auto_quantity_fill", False):
        # `_auto_quantity_fill` est un drapeau LOCAL (jamais persisté), le pop
        # est correct ici ; `quantity` en revanche est un vrai slot persisté.
        next_payload["quantity"] = None

    form_data: Dict[str, Any] = dict(existing_form_data or {})
    if next_payload.get("product") not in (None, "", [], {}):
        form_data.setdefault("product", next_payload.get("product"))
    if unit not in (None, "", [], {}):
        form_data.setdefault("unit", unit)

    # Prefill default deadline (+30 days) but keep it editable — uniquement
    # s'il n'y a VRAIMENT rien (ni dans le payload, ni déjà collecté par le
    # formulaire en cours).
    if (
        not next_payload.get("deadline")
        and not next_payload.get("deadline_date")
        and not form_data.get("deadline")
    ):
        default_deadline = (datetime.utcnow() + timedelta(days=30)).date().isoformat()
        form_data["deadline"] = default_deadline

    required_order = ("product", "price", "quantity", "deadline")
    missing_fields = [
        k for k in required_order if form_data.get(k) in (None, "", 0, [], {})
    ]
    last_missing_field = missing_fields[0] if missing_fields else None
    pending_patch = (
        set_pending_interaction(InteractionKind.ENTER_FIELD, field_name=last_missing_field)
        if last_missing_field
        else clear_pending_interaction("form_complete")
    )

    wm = dict(working_memory)
    wm["active_goal"] = "PROCUREMENT_CREATE_REQUEST"
    # None-overwrite : `working_memory` est réduit par `merge_dict` — un pop
    # sur le patch retourné ne supprime RIEN (l'ancienne valeur est conservée).
    # `buyer_request_waiting_choice` resté à True piégeait l'acheteur dans
    # l'état « en attente de choix » aux tours suivants (lu ligne ~240).
    for key in (
        "buyer_request_waiting_choice",
        "buyer_request_catalog_checked",
        "buyer_request_last_product",
    ):
        wm[key] = None

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
        **pending_patch,
        "vendor_selection_context": {"__reset__": True},
        "ag_ui_component": None,
    }


# =====================================================================
# BUYER REQUEST RESOLVER — catalog search then procurement escalation
# =====================================================================


async def buyer_request_resolver(
    state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Dict[str, Any]:
    """Search catalog first, then escalate to procurement on confirmation."""
    from ladini.graphs.agents.market_coach.services.domain.cart_service import (
        CartDomainService,
        ProductLookupUnavailable,
    )

    payload: Dict[str, Any] = dict(state.get("transaction_payload") or {})
    if not payload and state.get("extracted_entities"):
        payload = dict(state.get("extracted_entities") or {})

    phone = str(state.get("user_phone") or "")
    if not phone:
        return phone_missing_error()

    vendor_ctx = state.get("vendor_selection_context")
    vendor_ctx_active = bool(vendor_ctx) and not (
        isinstance(vendor_ctx, dict) and vendor_ctx.get("__reset__")
    )

    # Invalidate stale vendor_ctx if user is asking for a different product
    if vendor_ctx_active and isinstance(vendor_ctx, dict):
        ctx_product = str(vendor_ctx.get("product") or "").lower().strip()
        new_product = str(payload.get("product") or "").lower().strip()
        if new_product and ctx_product and new_product != ctx_product:
            logger.info(
                "buyer_request_resolver: stale vendor_ctx (product=%s) vs new request (product=%s) — clearing",
                ctx_product,
                new_product,
            )
            vendor_ctx_active = False

    logger.info(
        "buyer_request_resolver: vendor_ctx_active=%s, vendor_ctx_type=%s, payload_keys=%s",
        vendor_ctx_active,
        type(vendor_ctx).__name__ if vendor_ctx else "None",
        list((payload or {}).keys()),
    )
    if vendor_ctx_active:
        raw_selection = payload.get("selection_index")
        if raw_selection is None:
            raw_selection = (state.get("extracted_entities") or {}).get(
                "selection_index"
            )
        if raw_selection is None:
            raw_text = str(
                state.get("normalized_text") or state.get("user_query") or ""
            ).strip()
            if raw_text.isdigit():
                raw_selection = raw_text

        logger.info(
            "buyer_request_resolver: vendor_ctx has chosen_vendor=%s, raw_selection=%s",
            bool(vendor_ctx.get("chosen_vendor"))
            if isinstance(vendor_ctx, dict)
            else False,
            raw_selection,
        )

        if raw_selection is not None:
            next_payload = dict(payload)
            next_payload["selection_index"] = raw_selection
            next_state = dict(state)
            next_state["current_goal"] = "BUYER_ADD_TO_CART"
            next_state["transaction_payload"] = next_payload
            logger.info(
                "buyer_request_resolver: routing to cart_management with selection_index=%s",
                raw_selection,
            )
            return await cart_management(next_state, mc_runtime)

        # Vendor ctx active with chosen_vendor but no selection → ask for quantity
        if isinstance(vendor_ctx, dict) and vendor_ctx.get("chosen_vendor"):
            next_state = dict(state)
            next_state["current_goal"] = "BUYER_ADD_TO_CART"
            logger.info(
                "buyer_request_resolver: vendor already chosen, routing to cart_management for quantity"
            )
            return await cart_management(next_state, mc_runtime)

    stable_entities = state.get("stable_entities") or {}
    working_memory = dict(state.get("working_memory") or {})
    normalized_text = (
        str(state.get("normalized_text") or state.get("user_query") or "")
        .strip()
        .lower()
    )
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
        payload.pop("quantity", None)

    def _escalate(
        message: str,
        unit_hint: Optional[str] = None,
        existing_form_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return build_procurement_escalation(
            payload,
            working_memory,
            product_name,
            unit_hint or unit,
            message,
            existing_form_data=existing_form_data,
        )

    # "prix plafond" (jamais "prix minimum") : c'est le prix MAXIMUM que
    # l'acheteur accepte de payer — les producteurs doivent proposer À ou EN
    # DESSOUS de ce plafond (enchère inversée). Le récap (confirmation_summary.py,
    # goal PROCUREMENT_CREATE_REQUEST) et le label de champ
    # (interpreter/intent.py "price": "prix plafond proposé") utilisent déjà ce
    # terme — dire "prix minimum" ici contredisait le récap affiché juste après
    # avec le MÊME chiffre, ce qui semait la confusion (bug vécu en prod).
    _ESCALATION_MSG = (
        "Très bien, lançons un appel d'offres. "
        "J'aurai besoin du prix plafond que vous êtes prêt à payer, de la quantité souhaitée et d'une date limite."
    )

    # --- Direct escalation triggers ---
    if normalized_text in ESCALATE_KEYWORDS and product_name:
        return _escalate(_ESCALATION_MSG)

    if (
        current_goal == "PROCUREMENT_CREATE_REQUEST"
        and state.get("active_form") == "AUCTION_CREATE"
    ):
        # Ré-entrée dans un formulaire déjà actif : préserver le progrès déjà
        # collecté (prix/quantité/date déjà répondus par form_node) au lieu de
        # repartir d'un form_data quasi vide à chaque tour.
        return _escalate(_ESCALATION_MSG, existing_form_data=state.get("form_data"))

    # --- Waiting-choice state (no catalog stock, user asked if they want procurement) ---
    waiting_choice = bool(working_memory.get("buyer_request_waiting_choice"))
    interpreted_event = str(state.get("interpreted_event") or "").upper()

    if waiting_choice:
        # (2026-09-03, refonte transactionnelle, mandat §10) : ce choix est
        # déjà posé via `PendingInteraction(kind=CONFIRM_ACTION)` (voir plus
        # bas dans ce fichier, là où `buyer_request_waiting_choice=True` est
        # écrit) — `interpreted_event` (CONFIRM/REJECT) est donc DÉJÀ le
        # signal canonique fiable, produit par le MÊME contrat fast-path/LLM
        # que toute autre confirmation (`_CONFIRM_EXACT_PHRASES`,
        # `interpreter/routing.py`). Le domaine ne compare plus jamais le
        # texte utilisateur lui-même — un 2e moteur de reconnaissance
        # oui/non (une liste locale, vocabulaire plus étroit et désynchronisé
        # de `_CONFIRM_EXACT_PHRASES`) a été supprimé.
        if interpreted_event == "CONFIRM" or normalized_text in ESCALATE_KEYWORDS:
            return _escalate(_ESCALATION_MSG)
        if interpreted_event == "REJECT":
            wm = dict(working_memory)
            # None-overwrite (merge_dict) — voir explication plus haut : un pop
            # ici laissait `buyer_request_waiting_choice` actif après un refus.
            for key in (
                "buyer_request_waiting_choice",
                "buyer_request_catalog_checked",
                "buyer_request_last_product",
            ):
                wm[key] = None
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
            **set_pending_interaction(InteractionKind.ENTER_FIELD, field_name="product"),
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

    # If we have product + quantity + it looks like an add-to-cart intent,
    # delegate directly to cart_management (skips the vendor menu when only
    # one vendor exists, otherwise cart_management will show the menu).
    if quantity not in (None, "", 0):
        logger.info(
            "buyer_request_resolver: bridging to cart_management (product+qty present)"
        )
        synthetic = dict(state)
        syn_payload = dict(payload)
        syn_payload.setdefault("product", product_name)
        syn_payload.setdefault("quantity", quantity)
        if unit:
            syn_payload.setdefault("unit", unit)
        synthetic["transaction_payload"] = syn_payload
        synthetic["current_goal"] = "BUYER_ADD_TO_CART"
        return await cart_management(synthetic, mc_runtime)

    # Voir ProductLookupUnavailable : une panne de la recherche catalogue ne
    # doit jamais devenir « aucun produit disponible » — ici c'est encore plus
    # trompeur qu'au panier, puisque ce flux enchaînerait sur une proposition
    # d'appel d'offres pour un produit qui est peut-être bien en stock.
    try:
        vendors, has_multiple = await cart_service.resolve_product_vendors(
            phone, str(product_name)
        )
    except ProductLookupUnavailable:
        return {
            "status": "ERROR",
            "response_strategy": "ERROR",
            "final_response": (
                f"🔌 Je n'arrive pas à consulter le catalogue pour « *{product_name}* » "
                "en ce moment — c'est un souci technique de notre côté, pas une "
                "absence de stock.\n\nRéessayez dans un instant."
            ),
            "working_memory": working_memory,
            "ag_ui_component": None,
        }

    # Un match qui n'est QUE trigram (pas de sous-texte réel entre le terme
    # cherché et le nom trouvé — voir _is_confident_product_match) n'est pas
    # un "peut-être" à faire confirmer : c'est du bruit. Incident réel
    # (2026-08-14) : "oeufs" fuzzy-matchait "Bœuf" alors que ni œufs ni
    # laitue n'existent en base — proposer "Bœuf, c'est bien ça ?" pour une
    # recherche d'œufs est aussi trompeur qu'y répondre directement. On
    # écarte ces matches et on retombe sur le flux "produit introuvable"
    # existant (propose un appel d'offres) plutôt que d'inventer une
    # suggestion. Voir [[buyer-search-fuzzy-match-safety-2026-08]].
    confident_vendors = [v for v in vendors if v.get("match_confident", True)]
    if len(confident_vendors) != len(vendors):
        logger.warning(
            "buyer_request_resolver: dropped %d low-confidence match(es) for '%s' — treating as not found",
            len(vendors) - len(confident_vendors),
            product_name,
        )
    vendors = confident_vendors
    has_multiple = len(vendors) > 1

    if vendors and not has_multiple:
        # Single vendor — skip the selection menu and go directly to quantity
        ref = vendors[0]

        # --- MULTI-TARIFICATION : montrer les conditionnements AVANT toute
        # question de quantité (incident réel 2026-09-09) ---
        # Ce flux demandait « Quelle quantité ? » à l'aveugle même quand le
        # produit a plusieurs paliers de prix/conditionnement : l'acheteur
        # répondait « 11 L » sans savoir qu'on ne vend qu'en paquets de 5 L /
        # 10 L, et l'interpréteur mappait ce « 11 L » en silence sur le palier
        # le plus proche. `cart_management` porte déjà la logique « palier
        # avant quantité » (voir flows/buyer/cart.py, bloc « TIER RESOLUTION
        # FIRST » : il affiche le menu de conditionnements et pose
        # `tier_selection_context`). On lui délègue au lieu de dupliquer —
        # exactement comme le bridge « produit + quantité » plus haut.
        _ref_tiers = ref.get("pricing_tiers")
        if isinstance(_ref_tiers, list) and _ref_tiers:
            logger.info(
                "buyer_request_resolver: single vendor '%s' for '%s' is multi-tier "
                "— bridging to cart_management to show the packaging menu first",
                ref.get("vendor_name"),
                product_name,
            )
            synthetic = dict(state)
            syn_payload = dict(payload)
            syn_payload.setdefault("product", product_name)
            if unit:
                syn_payload.setdefault("unit", unit)
            synthetic["transaction_payload"] = syn_payload
            synthetic["current_goal"] = "BUYER_ADD_TO_CART"
            # On vient de résoudre les vendeurs (`resolve_product_vendors` ci-
            # dessus) — les passer à `cart_management` lui évite un SECOND
            # `search_products` identique (clé d'idempotence différente, donc
            # non dédupliqué : ~5 s + une requête catalogue de plus sur chaque
            # découverte multitarifaire). Clé transitoire, jamais renvoyée
            # dans un patch d'état, donc invisible au checkpointer.
            synthetic["_prefetched_vendors"] = vendors
            return await cart_management(synthetic, mc_runtime)

        vendor_label = ref.get("vendor_name") or "un producteur"
        unit_hint = ref.get("unit") or "KG"
        price_hint = ref.get("price")
        available_qty = ref.get("available_qty")
        price_info = (
            f" à *{_fmt_num(price_hint)} FCFA/{unit_hint}*" if price_hint else ""
        )
        qty_info = (
            f" (disponible : {_fmt_num(available_qty)} {unit_hint})"
            if available_qty
            else ""
        )
        payload["product"] = product_name
        wm = dict(working_memory)
        wm.update(
            {
                "buyer_request_catalog_checked": True,
                "buyer_request_last_product": product_name,
            }
        )
        extras_hint = additional_products_hint(payload, state)

        logger.info(
            "buyer_request_resolver: single vendor '%s' for '%s' — skipping menu, asking quantity",
            ref.get("vendor_name"),
            product_name,
        )
        vendor_ctx_seed = {
            "product": product_name,
            "vendors": vendors,
            "chosen_vendor": ref,
            "requested_quantity": None,
            "requested_unit": unit,
            "available_mapping_kind": "product_vendor",
        }
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"),
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": (
                f"✅ *{product_name}* est disponible chez *{vendor_label}*{price_info}{qty_info}.\n\n"
                f"📦 Quelle quantité souhaitez-vous ?\n"
                f"💡 _Exemples : 50 {unit_hint.lower()}, 2 sacs, 100 kg..._"
                + extras_hint
            ),
            "transaction_payload": payload,
            "vendor_selection_context": vendor_ctx_seed,
            "current_goal": "BUYER_ADD_TO_CART",
            "ag_ui_component": None,
            "working_memory": wm,
        }

    if vendors:
        extra_context = {"requested_quantity": quantity, "requested_unit": unit}
        menu_patch, _menu = cart_service.build_product_selection_menu(
            str(product_name),
            vendors,
            extra_context=extra_context,
            post_hint="💡 Si aucun produit ne vous convient, répondez *appel* pour lancer une demande spéciale aux producteurs.",
            phone=phone,
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

    # --- No stock: capter la demande non satisfaite + proposer un appel d'offres ---
    # Le produit n'existe pas au catalogue : on journalise ce que l'acheteur
    # cherche (agrégation globale) pour piloter le sourcing / l'ouverture de
    # nouvelles catégories. Best-effort, jamais bloquant.
    try:
        await ModerationGateway(mc_runtime).record_demand_signal(
            phone=str(phone or ""),
            raw_query=str(product_name),
            normalized_term=str(product_name),
            zone_id=state.get("zone_id"),
        )
    except Exception as demand_exc:
        logger.debug("record_demand_signal failed (non-blocking): %s", demand_exc)

    wm = dict(working_memory)
    wm.update(
        {
            "buyer_request_catalog_checked": True,
            "buyer_request_waiting_choice": True,
            "buyer_request_last_product": product_name,
        }
    )
    msg = (
        f"📭 Aucun produit disponible pour « *{product_name}* » dans notre catalogue.\n\n"
        "Souhaitez-vous lancer un *appel d'offres* pour que les producteurs "
        "vous fassent des propositions ?\n\n"
        "👉 Répondez *oui* pour lancer, ou *non* pour chercher autre chose."
    )
    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation"),
        "response_strategy": "ASK_MISSING_FIELD",
        "final_response": msg,
        "ag_ui_component": None,
        "transaction_payload": payload,
        "working_memory": wm,
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
        msg = result.get("message") or "Impossible de charger les propositions reçues."
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
            "final_response": "Aucune proposition n'a encore été déposée sur vos appels d'offres.",
            "ag_ui_component": None,
        }

    mapping: Dict[str, str] = {}
    lines = ["📥 *Propositions reçues sur vos appels d'offres :*"]
    for i, bid in enumerate(data, start=1):
        bid_id = str(bid.get("bid_id") or bid.get("id") or "")
        producer_name = (
            bid.get("producer_name") or bid.get("seller_name") or "Producteur"
        )
        price = bid.get("offered_price") or bid.get("price") or "?"
        product_name = bid.get("product") or bid.get("product_name") or "?"
        status = bid.get("status") or "PENDING"
        lines.append(
            f"\n*{i}. {producer_name}* — {product_name}\n💰 {price} FCFA — Statut: {status}"
        )
        mapping[str(i)] = bid_id

    menu = result.get("formatted_menu") or "\n".join(lines)
    candidates = [
        f"{b.get('producer_name') or b.get('seller_name') or 'Producteur'} ({b.get('product') or b.get('product_name') or 'Produit'})"
        for b in data
    ]
    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.SELECTION_MENU),
        "working_memory": {"bids_menu": menu},
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
            "final_response": "Aucune proposition disponible à accepter.",
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
            (
                b
                for b in data
                if b
                and (
                    target in str(b.get("producer_name")).lower()
                    or target in str(b.get("seller_name")).lower()
                )
            ),
            None,
        )

    if chosen:
        bid_id = chosen.get("bid_id") or chosen.get("id")
        if not bid_id:
            return {
                "status": "ERROR",
                "validation_errors": ["bid_not_resolved"],
                "response_strategy": "ERROR",
                "final_response": "Identifiant de la proposition introuvable sur l'élément sélectionné.",
                "ag_ui_component": None,
            }
        new_payload = dict(payload)
        new_payload["bid_id"] = str(bid_id)
        new_payload.pop("selection_index", None)
        new_payload.pop("selected_value", None)
        return {
            "status": "PLANNING",
            "transaction_payload": new_payload,
            "ag_ui_component": None,
        }

    # Fallback: re-display bids
    return await resolve_received_bids(mc_runtime, phone, payload)


__all__ = [
    "build_procurement_escalation",
    "buyer_request_resolver",
    "resolve_received_bids",
    "resolve_buyer_bid_pick",
]
