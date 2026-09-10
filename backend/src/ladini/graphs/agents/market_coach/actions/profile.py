"""Action handlers for the Profile domain."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from ladini.graphs.agents.market_coach.actions.profile_dto import (
    ProfileGetContextPayload,
    ProfileGetMcpUserPayload,
    ProfileGetTrustPayload,
    ProfileSetGeoPayload,
    ProfileSetPrefsPayload,
    ProfileSwitchRolePayload,
)
from ladini.graphs.agents.market_coach.actions.tooling import ToolResolver
from ladini.graphs.agents.market_coach.domain import DomainContext
from ladini.graphs.agents.market_coach.domain.profile import (
    ProfileGetContextCommand,
    ProfileGetMcpUserCommand,
    ProfileGetTrustCommand,
    ProfileService,
    ProfileSetGeoCommand,
    ProfileSetPrefsCommand,
    ProfileSwitchRoleCommand,
)
from ladini.graphs.agents.market_coach.registry import register_action


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


@register_action("PROFILE_GET_TRUST", mode="READ")
def prep_profile_get_trust(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation de la note de confiance.

    Outil MCP auto-enregistré : get_trust_score(user_id).
    Le résolveur de schéma mappe phone → user_id via _lookup_arg_value.
    """
    context = DomainContext.from_state(state)
    _dto = ProfileGetTrustPayload.from_payload(payload)
    command = ProfileGetTrustCommand(phone=context.phone)
    service = ProfileService(context=context)
    result = service.get_trust(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_trust_score")
    return tool_name, dict(result.tool_args)


@register_action("PROFILE_GET_CONTEXT", mode="READ")
def prep_profile_get_context(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare la lecture du contexte conversationnel (Outil MCP : get_user_context).

    Auparavant manquant — l'absence faisait crasher le module au chargement.
    """
    context = DomainContext.from_state(state)
    _dto = ProfileGetContextPayload.from_payload(payload)
    command = ProfileGetContextCommand(phone=context.phone)
    service = ProfileService(context=context)
    result = service.get_context(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_user_context")
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


@register_action("PROFILE_SWITCH_ROLE", mode="WRITE")
def prep_profile_switch_role(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    dto = ProfileSwitchRolePayload.from_payload(payload)
    command = ProfileSwitchRoleCommand(phone=context.phone, target_role=dto.target_role)
    service = ProfileService(context=context)
    result = service.switch_role(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "create_agent_action")
    return tool_name, dict(result.tool_args)
