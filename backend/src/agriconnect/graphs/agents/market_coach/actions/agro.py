"""Action handlers for the Agronomy domain."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from agriconnect.graphs.agents.market_coach.actions.common import require, require_phone
from agriconnect.graphs.agents.market_coach.actions.farm_dto import (
    FarmCreatePayload,
    FarmGetMyListPayload,
    FarmUpdatePayload,
)
from agriconnect.graphs.agents.market_coach.actions.tooling import ToolResolver
from agriconnect.graphs.agents.market_coach.domain import DomainContext
from agriconnect.graphs.agents.market_coach.domain.agro import (
    AgronomyService,
    FarmCreateCommand,
    FarmGetMyListCommand,
    FarmUpdateCommand,
)
from agriconnect.graphs.agents.market_coach.registry import register_action


@register_action("AGRO_GET_CYCLES", mode="READ")
def prep_agro_get_cycles(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare l'historique des cycles d'un domaine."""
    context = DomainContext.from_state(state)
    service = AgronomyService(context=context)
    result = service.get_cycles(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_crop_cycles")
    return tool_name, dict(result.tool_args)


@register_action("AGRO_GET_STANDARDS", mode="READ")
def prep_agro_get_standards(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation des fiches techniques."""
    context = DomainContext.from_state(state)
    service = AgronomyService(context=context)
    result = service.get_standards(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_crop_requirements")
    return tool_name, dict(result.tool_args)


@register_action("AGRO_GET_ECONOMICS", mode="READ")
def prep_agro_get_economics(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare le bilan financier d'une parcelle."""
    context = DomainContext.from_state(state)
    service = AgronomyService(context=context)
    result = service.get_economics(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_cycle_economics")
    return tool_name, dict(result.tool_args)


@register_action("AGRO_GET_RISKS", mode="READ")
def prep_agro_get_risks(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare l'analyse des risques sanitaires."""
    context = DomainContext.from_state(state)
    service = AgronomyService(context=context)
    result = service.get_risks(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_active_sanitary_risks")
    return tool_name, dict(result.tool_args)


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


@register_action("CROP_START_CYCLE", mode="WRITE")
def prep_crop_start_cycle(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    service = AgronomyService(context=context)
    result = service.start_cycle(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "create_crop_cycle")
    return tool_name, dict(result.tool_args)


@register_action("DECLARE_CROP_CYCLE", mode="WRITE")
def prep_declare_crop_cycle(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    service = AgronomyService(context=context)
    result = service.declare_crop_cycle(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "declare_future_production")
    return tool_name, dict(result.tool_args)


@register_action("CROP_RECORD_INTERVENTION", mode="WRITE")
def prep_crop_record_intervention(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    service = AgronomyService(context=context)
    result = service.record_intervention(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "log_intervention")
    return tool_name, dict(result.tool_args)


@register_action("CROP_RECORD_OBSERVATION", mode="WRITE")
def prep_crop_record_observation(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    service = AgronomyService(context=context)
    result = service.record_observation(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "add_growth_log")
    return tool_name, dict(result.tool_args)


@register_action("CROP_UPDATE_STAGE", mode="WRITE")
def prep_crop_update_stage(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    service = AgronomyService(context=context)
    result = service.update_stage(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "add_crop_growth_stage")
    return tool_name, dict(result.tool_args)


@register_action("CROP_UPDATE_SOIL", mode="WRITE")
def prep_crop_update_soil(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    service = AgronomyService(context=context)
    result = service.update_soil(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "update_soil_profile")
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
