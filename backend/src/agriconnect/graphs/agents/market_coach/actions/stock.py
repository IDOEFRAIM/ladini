"""Action handlers for the Stock domain."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from agriconnect.graphs.agents.market_coach.actions.common import (
    is_update_mode,
    normalize_quantity_to_kg,
    require_current_entity,
)
from agriconnect.graphs.agents.market_coach.actions.stock_dto import (
    StockUpdateLevelPayload,
)
from agriconnect.graphs.agents.market_coach.actions.tooling import ToolResolver
from agriconnect.graphs.agents.market_coach.domain import DomainContext
from agriconnect.graphs.agents.market_coach.domain.stock import (
    StockService,
    StockUpdateLevelCommand,
)
from agriconnect.graphs.agents.market_coach.registry import register_action


@register_action("STOCK_GET_SUMMARY", mode="READ")
def prep_stock_get_summary(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare l'appel pour l'inventaire global structuré ferme par ferme.

    Outil MCP : get_stocks(phone?).
    Depuis la refonte d'identité, `get_stocks` accepte explicitement `phone`
    ou `producer_id` et ne réutilise plus le champ détourné `farm_id`.
    """
    context = DomainContext.from_state(state)
    service = StockService(context=context)
    result = service.get_summary(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_stocks")
    return tool_name, dict(result.tool_args)


@register_action("STOCK_GET_DETAIL", mode="READ")
def prep_stock_get_detail(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare l'appel pour l'inventaire détaillé d'une exploitation.

    Outil MCP : get_farm_stocks(farm_id). Phone n'est pas attendu.
    """
    context = DomainContext.from_state(state)
    service = StockService(context=context)
    result = service.get_detail(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_farm_stocks")
    return tool_name, dict(result.tool_args)


@register_action("STOCK_GET_MOVEMENTS", mode="READ")
def prep_stock_get_movements(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation de la traçabilité d'un lot.

    Outil MCP : get_stock_movements(stock_id, limit?).
    Phone n'est PAS un paramètre MCP.
    """
    context = DomainContext.from_state(state)
    service = StockService(context=context)
    result = service.get_movements(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_stock_movements")
    return tool_name, dict(result.tool_args)


@register_action("STOCK_REGISTER_HARVEST", mode="WRITE")
def prep_stock_register_harvest(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    service = StockService(context=context)
    result = service.register_harvest(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "add_stock")
    return tool_name, dict(result.tool_args)


@register_action("STOCK_RECORD_MOVEMENT", mode="WRITE")
def prep_stock_record_movement(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    service = StockService(context=context)
    result = service.record_movement(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "add_stock_movement_by_id")
    return tool_name, dict(result.tool_args)


@register_action("STOCK_ADJUST", mode="WRITE")
def prep_stock_adjust(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    service = StockService(context=context)
    result = service.adjust(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "adjust_stock_by_id")
    return tool_name, dict(result.tool_args)


@register_action("STOCK_REMOVE_PARTIAL", mode="WRITE")
def prep_stock_remove_partial(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    service = StockService(context=context)
    result = service.remove_partial(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "remove_stock_by_id")
    return tool_name, dict(result.tool_args)


@register_action("STOCK_DELETE", mode="WRITE")
def prep_stock_delete(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    service = StockService(context=context)
    result = service.delete(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "delete_stock_by_id")
    return tool_name, dict(result.tool_args)


@register_action("STOCK_UPDATE_LEVEL", mode="WRITE")
def prep_stock_update_level(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    if not is_update_mode(state):
        raise ValueError("STOCK_UPDATE_LEVEL doit être invoqué en mode update.")

    entity = require_current_entity(state, intent="STOCK_UPDATE_LEVEL")
    dto = StockUpdateLevelPayload.from_state_and_payload(payload=payload, entity=entity)

    context = DomainContext.from_state(state)
    if not context.phone:
        raise ValueError(
            "Le numéro de téléphone du producteur est requis pour mettre à jour le stock."
        )

    # Application-layer translation: DTO -> Command
    quantity_kg, _ = normalize_quantity_to_kg(dto.quantity, dto.unit)
    command = StockUpdateLevelCommand(
        producer_id=context.phone,
        stock_id=dto.stock_id,
        new_quantity_kg=quantity_kg,
        reason=dto.reason or "Ajustement de niveau via update",
    )

    service = StockService(context=context)
    result = service.update_level(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "adjust_stock_by_id")
    return tool_name, dict(result.tool_args)
