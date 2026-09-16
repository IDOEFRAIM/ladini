from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from ladini.graphs.agents.market_coach.actions.tooling import ToolId
from ladini.graphs.agents.market_coach.domain.model import (
    DomainContext,
    DomainResult,
)


@dataclass(frozen=True)
class ProfileGetMcpUserCommand:
    phone: str


@dataclass(frozen=True)
class ProfileSetGeoCommand:
    phone: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class ProfileSetPrefsCommand:
    phone: str
    language: str
    allow_voice: bool = True


@dataclass
class ProfileService:
    """(2026-09-14, Deep Intent Architecture Cleanup) : `get_trust`/
    `get_context`/`switch_role` SUPPRIMÉES avec leurs intents
    (`get_trust_score`/`get_user_context`/`create_agent_action` inexistants
    comme outils MCP, PROFILE_SWITCH_ROLE obsolète — refonte double-rôle)."""

    context: DomainContext

    def get_mcp_user(self, command: ProfileGetMcpUserCommand) -> DomainResult:
        args: Dict[str, Any] = {"phone": str(command.phone)}
        return DomainResult(tool_id=ToolId.GET_USER_BY_PHONE, tool_args=args)

    def set_geo(self, command: ProfileSetGeoCommand) -> DomainResult:
        # `phone`, pas `user_id` : update_geo_location résout l'UUID en interne
        # (l'appelant conversationnel ne connaît que le numéro de téléphone).
        args: Dict[str, Any] = {
            "phone": str(command.phone),
            "lat": float(command.latitude),
            "lon": float(command.longitude),
        }
        return DomainResult(tool_id=ToolId.UPDATE_GEO_LOCATION, tool_args=args)

    def set_prefs(self, command: ProfileSetPrefsCommand) -> DomainResult:
        args: Dict[str, Any] = {
            "user_id": str(command.phone),
            "advice_time": str(command.language),
            "enabled": bool(command.allow_voice),
        }
        return DomainResult(tool_id=ToolId.UPDATE_COMMUNICATION_PREFS, tool_args=args)
