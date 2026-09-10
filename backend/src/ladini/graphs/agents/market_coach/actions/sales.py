"""Action handlers for the Sales & Marketplace domain."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from ladini.graphs.agents.market_coach.actions.common import (
    is_update_mode,
    require_current_entity,
)
from ladini.graphs.agents.market_coach.actions.sales_dto import (
    MarketGetRequestsPayload,
    SalesListOrdersPayload,
    SalesPublishProductPayload,
    SalesRecordDirectPayload,
    SalesUpdateProductPayload,
)
from ladini.graphs.agents.market_coach.actions.tooling import ToolResolver
from ladini.graphs.agents.market_coach.domain import DomainContext
from ladini.graphs.agents.market_coach.domain.sales import (
    MarketGetRequestsCommand,
    SalesListOrdersCommand,
    SalesPublishProductCommand,
    SalesRecordDirectCommand,
    SalesService,
    SalesUpdateProductCommand,
    SalesUpdateProductionCommand,
)
from ladini.graphs.agents.market_coach.registry import register_action


@register_action("SALES_GET_CATALOG", mode="READ")
def prep_sales_get_catalog(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation du catalogue, des stocks et des productions futures."""
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.get_catalog(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_stocks")
    return tool_name, dict(result.tool_args)


@register_action("MARKET_BROWSE_REQUESTS", mode="READ")
def prep_market_get_requests(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare la liste des appels d'offres du marché."""
    context = DomainContext.from_state(state)
    if not context.phone:
        raise ValueError(
            "Le numéro de téléphone de l'utilisateur est requis pour consulter les appels d'offres."
        )

    dto = MarketGetRequestsPayload.from_payload(payload)

    command = MarketGetRequestsCommand(
        phone=context.phone,
        status=dto.status or "OPEN",
        view_mode="MARKETPLACE",
        product_name=dto.product_name,
        zone_name=dto.zone_name,
    )

    service = SalesService(context=context)
    result = service.get_market_requests(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_auctions")
    return tool_name, dict(result.tool_args)


@register_action("MARKET_GET_REQUEST_DETAIL", mode="READ")
def prep_market_get_request_detail(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation des offres reçues sur sa propre demande."""
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.get_request_detail(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_auctions_bids")
    return tool_name, dict(result.tool_args)


@register_action("MARKET_GET_MY_PROPOSALS", mode="READ")
def prep_market_get_my_proposals(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare le suivi des propositions envoyées par le producteur."""
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.get_my_proposals(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_my_active_bids")
    return tool_name, dict(result.tool_args)


@register_action("MARKET_SNAPSHOT", mode="READ")
def prep_market_snapshot(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation des cours du marché local."""
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.market_snapshot(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_market_snapshot")
    return tool_name, dict(result.tool_args)


@register_action("MARKET_SNAPSHOT_ZONAL", mode="READ")
def prep_market_snapshot_zonal(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare l'analyse des tendances locales."""
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.market_snapshot_zonal(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_zone_market_overview")
    return tool_name, dict(result.tool_args)


@register_action("DASHBOARD_PRODUCER", mode="READ")
def prep_dashboard_producer(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare le chargement du tableau de bord d'exploitation."""
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.dashboard_producer(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_producer_dashboard")
    return tool_name, dict(result.tool_args)


@register_action("SEARCH_PRODUCTS", mode="READ")
def prep_search_products(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare la recherche par mot-clé dans le catalogue."""
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.search_products(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "search_products")
    return tool_name, dict(result.tool_args)


@register_action("SEARCH_NEARBY", mode="READ")
def prep_search_nearby(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare la recherche de proximité GPS."""
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.search_nearby(state, payload)
    tool_name = ToolResolver.resolve_name(
        result.tool_id or "get_all_zone_market_overview"
    )
    return tool_name, dict(result.tool_args)


@register_action("VALIDATE_PRICE", mode="READ")
def prep_validate_price(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare la vérification de cohérence de prix."""
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.validate_price(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "check_price_anomaly")
    return tool_name, dict(result.tool_args)


@register_action("SALES_PUBLISH_PRODUCT", mode="WRITE")
def prep_sales_publish_product(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    if not context.phone:
        raise ValueError(
            "Le numéro de téléphone du producteur est requis pour publier un produit."
        )

    dto = SalesPublishProductPayload.from_payload(payload)

    command = SalesPublishProductCommand(
        producer_id=context.phone,
        product=dto.product,
        quantity=dto.quantity,
        unit=dto.unit,
        price=dto.price,
        description=dto.description,
        category_label=dto.category_label,
        pricing_tiers=dto.pricing_tiers,
    )

    service = SalesService(context=context)
    result = service.publish_product(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "create_product")
    return tool_name, dict(result.tool_args)


@register_action("SALES_RECORD_DIRECT", mode="WRITE")
def prep_sales_record_direct(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    if not context.phone:
        raise ValueError(
            "Le numéro de téléphone du producteur est requis pour enregistrer une vente."
        )

    dto = SalesRecordDirectPayload.from_payload(payload)

    command = SalesRecordDirectCommand(
        producer_id=context.phone,
        product=dto.product,
        quantity=dto.quantity,
        unit=dto.unit,
        price=dto.price,
    )

    service = SalesService(context=context)
    result = service.record_direct_sale(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "record_sale")
    return tool_name, dict(result.tool_args)


@register_action("SALES_PLACE_BID", mode="WRITE")
def prep_sales_place_bid(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.place_bid(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "place_bid")
    return tool_name, dict(result.tool_args)


@register_action("SALES_ACCEPT_CONTRACT", mode="WRITE")
def prep_sales_accept_contract(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.accept_contract(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "commit_staged_transaction")
    return tool_name, dict(result.tool_args)


@register_action("SALES_UPDATE_PRODUCT", mode="WRITE")
def prep_sales_update_product(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    if not is_update_mode(state):
        raise ValueError("SALES_UPDATE_PRODUCT doit être invoqué en mode update.")

    entity = require_current_entity(state, intent="SALES_UPDATE_PRODUCT")
    dto = SalesUpdateProductPayload.from_state_and_payload(
        payload=payload, entity=entity
    )

    context = DomainContext.from_state(state)
    if not context.phone:
        raise ValueError(
            "Le numéro de téléphone du producteur est requis pour mettre à jour le produit."
        )

    command = SalesUpdateProductCommand(
        producer_id=context.phone,
        product_id=dto.product_id,
        price=dto.price,
        quantity=dto.quantity,
        name=dto.name,
        unit=dto.unit,
        pricing_tiers=dto.pricing_tiers,
    )

    service = SalesService(context=context)
    result = service.update_product(command)
    tool_name = ToolResolver.resolve_name(
        result.tool_id or "update_product_price_and_qty"
    )
    return tool_name, dict(result.tool_args)


def _coerce_opt_float(value: Any) -> Any:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return None


@register_action("SALES_UPDATE_PRODUCTION", mode="WRITE")
def prep_sales_update_production(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Mise à jour d'un lot futur (MarketOffer) : prix/quantité/nom/unité/date/type."""
    context = DomainContext.from_state(state)
    if not context.phone:
        raise ValueError(
            "Le numéro de téléphone du producteur est requis pour mettre à jour la production."
        )

    # cycle_id : posé par la sélection (memory.py mappe la ligne choisie), avec
    # plusieurs alias possibles selon le canal de sélection.
    cycle_id = (
        payload.get("cycle_id")
        or payload.get("market_offer_id")
        or payload.get("offer_id")
        or payload.get("selected_value")
    )
    if not cycle_id:
        raise ValueError(
            "Sélectionnez d'abord la production à modifier (numéro dans la liste)."
        )

    # Nouveau nom éventuel : `product` (slot canonique). Rejeté s'il vaut un mot
    # de type (culture/élevage) — déjà filtré en amont par _sanitize_product_candidate.
    command = SalesUpdateProductionCommand(
        producer_id=context.phone,
        cycle_id=str(cycle_id),
        price=_coerce_opt_float(payload.get("price")),
        quantity=_coerce_opt_float(payload.get("quantity")),
        product_label=(
            str(payload["product"]).strip() if payload.get("product") else None
        ),
        unit=(str(payload["unit"]).strip() if payload.get("unit") else None),
        estimated_available_at=(payload.get("estimated_available_at") or None),
        production_type=(payload.get("production_type") or None),
    )

    service = SalesService(context=context)
    result = service.update_production(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "update_production_fields")
    return tool_name, dict(result.tool_args)


@register_action("SALES_LIST_ORDERS", mode="READ")
def prep_sales_list_orders(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    if not context.phone:
        raise ValueError(
            "Le numéro de téléphone du producteur est requis pour consulter ses commandes."
        )

    dto = SalesListOrdersPayload.from_payload(payload)
    command = SalesListOrdersCommand(
        phone=context.phone,
        status=dto.status,
        limit=dto.limit,
    )

    service = SalesService(context=context)
    result = service.list_orders(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_producer_orders")
    return tool_name, dict(result.tool_args)


@register_action("PRODUCER_CONFIRM_DELIVERY_PAYMENT", mode="WRITE")
def prep_confirm_delivery_and_payment(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Clôture F1 (paiement à la livraison, 2026-09-04) — pas de
    Command/Payload DTO dédié : l'action ne porte qu'UN champ
    (`order_id`, déjà résolu par
    `flows/producer/flow.py::_resolve_order_for_delivery_payment` avant
    d'atteindre ce point), la machinerie DTO complète des autres actions
    SALES serait disproportionnée ici."""
    context = DomainContext.from_state(state)
    if not context.phone:
        raise ValueError(
            "Le numéro de téléphone du producteur est requis pour confirmer une livraison."
        )
    order_id = payload.get("order_id")
    if not order_id:
        raise ValueError("Identifiant de commande manquant.")
    tool_name = ToolResolver.resolve_name("confirm_delivery_and_payment")
    return tool_name, {"producer_phone": context.phone, "order_id": str(order_id)}


@register_action("PRODUCER_CANCEL_ORDER", mode="WRITE")
def prep_producer_cancel_order(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Annulation producteur d'une commande confirmée (Phase 5, décision
    produit #1). Même sobriété que `prep_confirm_delivery_and_payment` :
    `order_id` est déjà résolu par
    `flows/producer/flow.py::_resolve_order_for_cancellation`, et le motif
    reste du texte LIBRE (aucune taxonomie inventée — cf. le registre de
    décisions). Toute la règle métier vit dans
    `services/database/producer.py::cancel_confirmed_order`."""
    context = DomainContext.from_state(state)
    if not context.phone:
        raise ValueError(
            "Le numéro de téléphone du producteur est requis pour annuler une commande."
        )
    order_id = payload.get("order_id")
    if not order_id:
        raise ValueError("Identifiant de commande manquant.")
    reason = payload.get("reason") or payload.get("cancel_reason") or ""
    tool_name = ToolResolver.resolve_name("cancel_confirmed_order")
    return tool_name, {
        "producer_phone": context.phone,
        "order_id": str(order_id),
        "reason": str(reason).strip(),
    }


@register_action("SALES_UNPUBLISH_PRODUCT", mode="WRITE")
def prep_sales_unpublish_product(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Retrait d'un produit du catalogue (Product Completeness Phase 2,
    2026-09-04). Même sobriété volontaire que
    `prep_confirm_delivery_and_payment` ci-dessus : un seul champ
    (`product_id`, déjà résolu par
    `flows/producer/flow.py::_resolve_product_for_unpublish`). Toute la
    règle métier (refus si commandes actives, archivage doux vs suppression
    physique) vit dans `services/database/product.py::delete_product` —
    jamais réimplémentée ici."""
    context = DomainContext.from_state(state)
    if not context.phone:
        raise ValueError(
            "Le numéro de téléphone du producteur est requis pour retirer un produit."
        )
    product_id = payload.get("product_id")
    if not product_id:
        raise ValueError("Identifiant de produit manquant.")
    tool_name = ToolResolver.resolve_name("delete_product")
    return tool_name, {"phone": context.phone, "product_id": str(product_id)}
