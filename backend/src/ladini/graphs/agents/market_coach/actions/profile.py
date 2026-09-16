"""Action handlers for the Profile domain."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from ladini.graphs.agents.market_coach.actions.profile_dto import (
    ProfileGetMcpUserPayload,
    ProfileSetGeoPayload,
    ProfileSetPrefsPayload,
)
from ladini.graphs.agents.market_coach.actions.tooling import ToolResolver
from ladini.graphs.agents.market_coach.domain import DomainContext
from ladini.graphs.agents.market_coach.domain.profile import (
    ProfileGetMcpUserCommand,
    ProfileService,
    ProfileSetGeoCommand,
    ProfileSetPrefsCommand,
)
from ladini.graphs.agents.market_coach.registry import register_action

# (2026-09-14, Deep Intent Architecture Cleanup) : PROFILE_GET_TRUST/
# PROFILE_GET_CONTEXT/PROFILE_SWITCH_ROLE SUPPRIMÉS — `get_trust_score`/
# `get_user_context`/`create_agent_action` n'existent comme outil MCP nulle
# part, et la refonte double-rôle a rendu PROFILE_SWITCH_ROLE conceptuellement
# obsolète (voir intent.py pour l'audit complet).


@register_action("PROFILE_GET_MCP_USER", mode="READ")
def prep_profile_get_mcp_user(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare la résolution de profil par téléphone."""
    context = DomainContext.from_state(state)
    dto = ProfileGetMcpUserPayload.from_payload(payload)
    command = ProfileGetMcpUserCommand(phone=dto.phone)
    service = ProfileService(context=context)
    result = service.get_mcp_user(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_user_by_phone")
    return tool_name, dict(result.tool_args)


@register_action("PROFILE_SET_GEO", mode="WRITE")
def prep_profile_set_geo(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    dto = ProfileSetGeoPayload.from_payload(payload)
    command = ProfileSetGeoCommand(
        phone=context.phone, latitude=dto.latitude, longitude=dto.longitude
    )
    service = ProfileService(context=context)
    result = service.set_geo(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "update_geo_location")
    return tool_name, dict(result.tool_args)


@register_action("PROFILE_SET_PREFS", mode="WRITE")
def prep_profile_set_prefs(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    dto = ProfileSetPrefsPayload.from_payload(payload)
    command = ProfileSetPrefsCommand(
        phone=context.phone,
        language=dto.language,
        allow_voice=bool(dto.allow_voice) if dto.allow_voice is not None else True,
    )
    service = ProfileService(context=context)
    result = service.set_prefs(command)
    tool_name = ToolResolver.resolve_name(
        result.tool_id or "update_communication_prefs"
    )
    return tool_name, dict(result.tool_args)


