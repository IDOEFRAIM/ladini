"""Action handlers for the Sales & Marketplace domain."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from agriconnect.graphs.agents.market_coach.registry import register_action
from agriconnect.graphs.agents.market_coach.actions.tooling import ToolResolver
from agriconnect.graphs.agents.market_coach.actions.common import (
    is_update_mode,
    require_current_entity,
)
from agriconnect.graphs.agents.market_coach.actions.sales_dto import (
    MarketGetRequestsPayload,
    SalesListOrdersPayload,
    SalesPublishProductPayload,
    SalesRecordDirectPayload,
    SalesUpdateProductPayload,
)
from agriconnect.graphs.agents.market_coach.domain import DomainContext
from agriconnect.graphs.agents.market_coach.domain.sales import (
    MarketGetRequestsCommand,
    SalesListOrdersCommand,
    SalesPublishProductCommand,
    SalesRecordDirectCommand,
    SalesService,
    SalesUpdateProductCommand,
)

@register_action("SALES_GET_CATALOG", mode="READ")
def prep_sales_get_catalog(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation du catalogue, des stocks et des productions futures."""
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.get_catalog(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_stocks")
    return tool_name, dict(result.tool_args)


@register_action("MARKET_BROWSE_REQUESTS", mode="READ")
def prep_market_get_requests(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la liste des appels d'offres du marché."""
    context = DomainContext.from_state(state)
    if not context.phone:
        raise ValueError("Le numéro de téléphone de l'utilisateur est requis pour consulter les appels d'offres.")

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
def prep_market_get_request_detail(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation des offres reçues sur sa propre demande."""
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.get_request_detail(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_auctions_bids")
    return tool_name, dict(result.tool_args)


@register_action("MARKET_GET_MY_PROPOSALS", mode="READ")
def prep_market_get_my_proposals(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare le suivi des propositions envoyées par le producteur."""
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.get_my_proposals(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_my_active_bids")
    return tool_name, dict(result.tool_args)


@register_action("MARKET_SNAPSHOT", mode="READ")
def prep_market_snapshot(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation des cours du marché local."""
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.market_snapshot(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_market_snapshot")
    return tool_name, dict(result.tool_args)


@register_action("MARKET_SNAPSHOT_ZONAL", mode="READ")
def prep_market_snapshot_zonal(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare l'analyse des tendances locales."""
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.market_snapshot_zonal(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_zone_market_overview")
    return tool_name, dict(result.tool_args)


@register_action("DASHBOARD_PRODUCER", mode="READ")
def prep_dashboard_producer(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare le chargement du tableau de bord d'exploitation."""
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.dashboard_producer(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_producer_dashboard")
    return tool_name, dict(result.tool_args)


@register_action("SEARCH_PRODUCTS", mode="READ")
def prep_search_products(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la recherche par mot-clé dans le catalogue."""
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.search_products(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "search_products")
    return tool_name, dict(result.tool_args)


@register_action("SEARCH_NEARBY", mode="READ")
def prep_search_nearby(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la recherche de proximité GPS."""
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.search_nearby(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_all_zone_market_overview")
    return tool_name, dict(result.tool_args)


@register_action("VALIDATE_PRICE", mode="READ")
def prep_validate_price(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la vérification de cohérence de prix."""
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.validate_price(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "check_price_anomaly")
    return tool_name, dict(result.tool_args)


@register_action("SALES_PUBLISH_PRODUCT", mode="WRITE")
def prep_sales_publish_product(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    if not context.phone:
        raise ValueError("Le numéro de téléphone du producteur est requis pour publier un produit.")

    dto = SalesPublishProductPayload.from_payload(payload)

    command = SalesPublishProductCommand(
        producer_id=context.phone,
        product=dto.product,
        quantity=dto.quantity,
        unit=dto.unit,
        price=dto.price,
        description=dto.description,
        category_label=dto.category_label,
    )

    service = SalesService(context=context)
    result = service.publish_product(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "create_product")
    return tool_name, dict(result.tool_args)


@register_action("SALES_RECORD_DIRECT", mode="WRITE")
def prep_sales_record_direct(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    if not context.phone:
        raise ValueError("Le numéro de téléphone du producteur est requis pour enregistrer une vente.")

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
def prep_sales_place_bid(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.place_bid(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "place_bid")
    return tool_name, dict(result.tool_args)


@register_action("SALES_ACCEPT_CONTRACT", mode="WRITE")
def prep_sales_accept_contract(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    service = SalesService(context=context)
    result = service.accept_contract(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "commit_staged_transaction")
    return tool_name, dict(result.tool_args)


@register_action("SALES_UPDATE_PRODUCT", mode="WRITE")
def prep_sales_update_product(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    if not is_update_mode(state):
        raise ValueError("SALES_UPDATE_PRODUCT doit être invoqué en mode update.")

    entity = require_current_entity(state, intent="SALES_UPDATE_PRODUCT")
    dto = SalesUpdateProductPayload.from_state_and_payload(payload=payload, entity=entity)

    context = DomainContext.from_state(state)
    if not context.phone:
        raise ValueError("Le numéro de téléphone du producteur est requis pour mettre à jour le produit.")

    command = SalesUpdateProductCommand(
        producer_id=context.phone,
        product_id=dto.product_id,
        price=dto.price,
        quantity=dto.quantity,
    )

    service = SalesService(context=context)
    result = service.update_product(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "update_product_price_and_qty")
    return tool_name, dict(result.tool_args)


@register_action("SALES_LIST_ORDERS", mode="READ")
def prep_sales_list_orders(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    if not context.phone:
        raise ValueError("Le numéro de téléphone du producteur est requis pour consulter ses commandes.")

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
