from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from agriconnect.graphs.agents.market_coach.actions.tooling import ToolId
from agriconnect.graphs.agents.market_coach.domain.model import (
    DomainContext,
    DomainResult,
)


@dataclass(frozen=True)
class ProfileGetMcpUserCommand:
    phone: str


@dataclass(frozen=True)
class ProfileGetTrustCommand:
    phone: str


@dataclass(frozen=True)
class ProfileGetContextCommand:
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


@dataclass(frozen=True)
class ProfileSwitchRoleCommand:
    phone: str
    target_role: str


@dataclass
class ProfileService:
    context: DomainContext

    def get_mcp_user(self, command: ProfileGetMcpUserCommand) -> DomainResult:
        args: Dict[str, Any] = {"phone": str(command.phone)}
        return DomainResult(tool_id=ToolId.GET_USER_BY_PHONE, tool_args=args)

    def get_trust(self, command: ProfileGetTrustCommand) -> DomainResult:
        args: Dict[str, Any] = {"user_id": str(command.phone)}
        return DomainResult(tool_id=ToolId.GET_TRUST_SCORE, tool_args=args)

    def get_context(self, command: ProfileGetContextCommand) -> DomainResult:
        args: Dict[str, Any] = {"user_id": str(command.phone)}
        return DomainResult(tool_id=ToolId.GET_USER_CONTEXT, tool_args=args)

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

    def switch_role(self, command: ProfileSwitchRoleCommand) -> DomainResult:
        role = str(command.target_role).upper().strip()
        args: Dict[str, Any] = {
            "agent_name": "MarketCoach",
            "action_type": "PROFILE_SWITCH_ROLE",
            "payload": {"phone": str(command.phone), "target_role": role},
        }
        return DomainResult(tool_id=ToolId.CREATE_AGENT_ACTION, tool_args=args)
