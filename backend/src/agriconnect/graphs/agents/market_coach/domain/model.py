from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional

from agriconnect.graphs.agents.market_coach.actions.tooling import ToolId


@dataclass(frozen=True)
class DomainContext:
    """Immutable snapshot of the business context for a single action.

    It is derived from the agent state and exposes only domain-relevant
    fields so that services are decoupled from the raw graph state.
    """

    user_id: Optional[str]
    phone: Optional[str]
    role: Optional[str]
    language: Optional[str]
    region: Optional[str]
    organization: Optional[str]
    permissions: FrozenSet[str]
    tenant: Optional[str]
    timezone: Optional[str]

    @classmethod
    def from_state(
        cls, state: Mapping[str, Any], permissions: Optional[Iterable[str]] = None
    ) -> "DomainContext":
        perms: FrozenSet[str] = frozenset(
            str(p).strip() for p in (permissions or ()) if p
        )
        return cls(
            user_id=str(state.get("user_id")) or None,
            phone=str(state.get("user_phone")) or None,
            role=str(state.get("role") or state.get("user_role")) or None,
            language=str(state.get("language") or state.get("locale")) or None,
            region=str(state.get("region") or state.get("zone")) or None,
            organization=str(state.get("organization")) or None,
            permissions=perms,
            tenant=str(state.get("tenant")) or None,
            timezone=str(state.get("timezone")) or None,
        )


@dataclass(frozen=True)
class DomainEvent:
    """Base event type produced by domain services.

    Events are collected in ``DomainResult.events`` and can later be
    published on an EventBus without modifying services or handlers.
    """

    name: str
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionStarted(DomainEvent):
    pass


@dataclass(frozen=True)
class ActionCompleted(DomainEvent):
    pass


@dataclass(frozen=True)
class ActionFailed(DomainEvent):
    pass


@dataclass
class DomainResult:
    """Result of a domain service invocation.

    Handlers consume this object to obtain the tool identifier and
    arguments to forward to ToolProvider, as well as optional events,
    warnings and metadata.
    """

    tool_id: Optional[ToolId]
    tool_args: Dict[str, Any]
    events: List[DomainEvent] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
