"""Action handlers for the Procurement domain."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from agriconnect.graphs.agents.market_coach.registry import register_action
from agriconnect.graphs.agents.market_coach.actions.common import require, require_phone, normalize_quantity_to_kg
from agriconnect.graphs.agents.market_coach.actions.tooling import ToolResolver
from agriconnect.graphs.agents.market_coach.domain import DomainContext
from agriconnect.graphs.agents.market_coach.domain.procurement import (
    ProcurementService,
    ProcurementCreateRequestCommand,
    ProcurementSelectWinnerCommand,
    ProcurementAcceptOfferCommand,
)
from agriconnect.graphs.agents.market_coach.actions.procure_dto import (
    ProcurementCreateRequestPayload,
    ProcurementSelectWinnerPayload,
    ProcurementAcceptOfferPayload,
)

@register_action("PROCUREMENT_CREATE_REQUEST", mode="WRITE")
def prep_procurement_create_request(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    # Validation légère spécifique au handler (ex: présence de champs clés)
    require_phone(state)
    require(payload, "product")
    require(payload, "quantity")
    require(payload, "price")

    context = DomainContext.from_state(state)
    dto = ProcurementCreateRequestPayload.from_payload(payload)
    command = ProcurementCreateRequestCommand(
        phone=context.phone,
        product=dto.product,
        quantity=dto.quantity,
        unit=dto.unit,
        max_price=dto.price,
        deadline=dto.deadline,
        delivery_location=dto.delivery_location,
        delivery_deadline=dto.delivery_deadline,
        incoterm=dto.incoterm,
        auto_extend=bool(dto.auto_extend) if dto.auto_extend is not None else True,
        zone_name=dto.zone_name,
    )

    service = ProcurementService(context=context)
    result = service.create_request(command)

    tool_name = ToolResolver.resolve_name(result.tool_id or "create_auction")
    return tool_name, dict(result.tool_args)


@register_action("PROCUREMENT_SELECT_WINNER", mode="WRITE")
def prep_procurement_select_winner(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    require_phone(state)
    require(payload, "auction_id")
    require(payload, "bid_id")

    context = DomainContext.from_state(state)
    dto = ProcurementSelectWinnerPayload.from_payload(payload)
    command = ProcurementSelectWinnerCommand(
        phone=context.phone,
        auction_id=dto.auction_id,
        bid_id=dto.bid_id,
    )
    service = ProcurementService(context=context)
    result = service.select_winner(command)

    tool_name = ToolResolver.resolve_name(result.tool_id or "select_winning_bid")
    return tool_name, dict(result.tool_args)


@register_action("PROCUREMENT_ACCEPT_OFFER", mode="WRITE")
def prep_procurement_accept_offer(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    require_phone(state)
    require(payload, "bid_id")

    context = DomainContext.from_state(state)
    dto = ProcurementAcceptOfferPayload.from_payload(payload)
    command = ProcurementAcceptOfferCommand(
        phone=context.phone,
        bid_id=dto.bid_id,
    )
    service = ProcurementService(context=context)
    result = service.accept_offer(command)

    tool_name = ToolResolver.resolve_name(result.tool_id or "accept_bid")
    return tool_name, dict(result.tool_args)
