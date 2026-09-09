from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional

from agriconnect.graphs.agents.market_coach.actions.tooling import ToolId


def _opt_str(value: Any) -> Optional[str]:
    """Convertit en `str` non-vide, ou `None` — jamais la chaîne littérale
    "None" (2026-09-08, bug réel découvert en corrigeant la redondance
    `role`/`user_role`, voir `from_state` ci-dessous). `str(value) or None`
    est TOUJOURS vrai pour `value=None` (`str(None) == "None"`, une chaîne
    NON VIDE, donc truthy) : chaque champ optionnel de `DomainContext`
    prenait silencieusement la valeur "None" (texte) plutôt que `None`
    (Python) quand la donnée source était absente — un `if context.phone:`
    en aval aurait alors vu une valeur PRÉSENTE (la chaîne "None") pour une
    identité en réalité manquante."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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
            user_id=_opt_str(state.get("user_id")),
            phone=_opt_str(state.get("user_phone")),
            # (2026-09-08) `state.get("role")` retiré — c'était un doublon
            # figé de `user_role` (jamais rafraîchi après le 1er tour, voir
            # `core/state.py`) qui pouvait gagner à tort sur la valeur à
            # jour via un `or`. `user_role` est l'unique source.
            role=_opt_str(state.get("user_role")),
            language=_opt_str(state.get("language") or state.get("locale")),
            region=_opt_str(state.get("region") or state.get("zone")),
            organization=_opt_str(state.get("organization")),
            permissions=perms,
            tenant=_opt_str(state.get("tenant")),
            timezone=_opt_str(state.get("timezone")),
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
