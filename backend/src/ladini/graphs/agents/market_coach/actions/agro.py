"""Action handlers — production future (déclaration/mise à jour d'un lot
pas encore disponible) et exploitations (FARM_*).

(2026-09-14, Deep Intent Architecture Cleanup) : les handlers AGRO_GET_*/
CROP_START_CYCLE/CROP_RECORD_*/CROP_UPDATE_* sont SUPPRIMÉS — intents
correspondants retirés d'`INTENT_CONFIG` (aucune méthode DB, aucun outil
MCP exposé, voir le rapport du chantier). `AgronomyService` (domain/agro.py)
est nettoyée en conséquence — seules `declare_crop_cycle`/`update_production`
et les méthodes FARM_* restent, les autres n'avaient plus aucun appelant."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from ladini.graphs.agents.market_coach.actions.common import require, require_phone
from ladini.graphs.agents.market_coach.actions.farm_dto import (
    FarmCreatePayload,
    FarmGetMyListPayload,
    FarmUpdatePayload,
)
from ladini.graphs.agents.market_coach.actions.tooling import ToolResolver
from ladini.graphs.agents.market_coach.domain import DomainContext
from ladini.graphs.agents.market_coach.domain.agro import (
    AgronomyService,
    FarmCreateCommand,
    FarmGetMyListCommand,
    FarmUpdateCommand,
)
from ladini.graphs.agents.market_coach.registry import register_action


@register_action("FARM_GET_MY_LIST", mode="READ")
def prep_farm_get_my_list(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare la demande de listing des exploitations du producteur."""
    require_phone(state)
    context = DomainContext.from_state(state)
    _ = FarmGetMyListPayload.from_payload(payload)
    command = FarmGetMyListCommand(phone=context.phone or "")
    service = AgronomyService(context=context)
    result = service.get_my_farms(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_producer_farm")
    return tool_name, dict(result.tool_args)


@register_action("PRODUCTION_DECLARE_FUTURE", mode="WRITE")
def prep_production_declare_future(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    service = AgronomyService(context=context)
    result = service.declare_crop_cycle(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "declare_future_production")
    return tool_name, dict(result.tool_args)


@register_action("FARM_CREATE", mode="WRITE")
def prep_farm_create(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    require_phone(state)
    require(payload, "farm_name")
    require(payload, "zone")
    context = DomainContext.from_state(state)
    dto = FarmCreatePayload.from_payload(payload)
    command = FarmCreateCommand(
        phone=context.phone or "",
        farm_name=dto.farm_name,
        zone=dto.zone,
        surface=dto.surface,
    )
    service = AgronomyService(context=context)
    result = service.create_farm(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_or_create_farm")
    return tool_name, dict(result.tool_args)


@register_action("FARM_UPDATE", mode="WRITE")
def prep_farm_update(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    require_phone(state)
    require(payload, "farm_id")
    context = DomainContext.from_state(state)
    dto = FarmUpdatePayload.from_payload(payload)
    command = FarmUpdateCommand(
        phone=context.phone or "",
        farm_id=dto.farm_id,
        farm_name=dto.farm_name,
        surface=dto.surface,
    )
    service = AgronomyService(context=context)
    result = service.update_farm(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "update_farm")
    return tool_name, dict(result.tool_args)
