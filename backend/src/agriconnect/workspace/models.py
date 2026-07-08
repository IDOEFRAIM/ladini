"""Workspace — unité de contexte durable d'AgriConnect.

Toutes les décisions de routage et d'exécution partent du Workspace,
jamais du dernier message seul. Un Workspace par utilisateur (clé = téléphone)
et un seul agent (MarketCoach).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

@dataclass(slots=True)
class Workspace:
    """État durable d'une conversation utilisateur.

    Champs:
        workspace_id: identifiant stable (numéro de téléphone normalisé).
        workspace_type: "producer" | "buyer" (rôle marché de l'utilisateur).
        active_goal: objectif courant (ex: "SALES_PUBLISH_PRODUCT").
        active_form: formulaire en cours (ex: "PRODUCT_CREATE") ou None.
        tunnel_locked: booléen indiquant si un tunnel est actif.
        metadata: snapshot d'état agent + mémoire conversationnelle (JSONB).
        updated_at: epoch seconds du dernier write.
    """

    workspace_id: str
    workspace_type: str = "producer"
    active_goal: str = ""
    active_form: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    agent_state: Dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)
    tunnel_locked: bool = False
    _is_dirty: bool = field(default=False, init=False, repr=False, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "workspace_type": self.workspace_type,
            "active_agent": self.active_agent,
            "active_goal": self.active_goal,
            "active_form": self.active_form,
            "locked_agent": self.locked_agent,
            "metadata": self.metadata,
            "langgraph_state": self.agent_state,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Workspace":
        return cls(
            workspace_id=str(data["workspace_id"]),
            workspace_type=str(data.get("workspace_type") or "producer"),
            active_goal=str(data.get("active_goal") or ""),
            active_form=data.get("active_form"),
            metadata=dict(data.get("metadata") or {}),
            agent_state=dict(data.get("langgraph_state") or {}),
            updated_at=float(data.get("updated_at") or time.time()),
            tunnel_locked=bool(data.get("locked_agent")),
        )

    def __repr__(self) -> str:
        return (
            f"Workspace(id={self.workspace_id!r} type={self.workspace_type!r}"
            f" agent={self.active_agent!r} goal={self.active_goal!r}"
            f" form={self.active_form!r} locked={self.locked_agent!r})"
        )

    @property
    def active_agent(self) -> str:
        """Mono-agent permanent."""
        return "market"

    @property
    def locked_agent(self) -> Optional[str]:
        return "market" if self.tunnel_locked else None

    @locked_agent.setter
    def locked_agent(self, value: Optional[str]) -> None:  # pragma: no cover - compat
        self.tunnel_locked = bool(value)

    @property
    def is_fresh(self) -> bool:
        """True when this workspace has never had a LangGraph checkpoint saved."""
        return not bool(self.agent_state)

    def touch(self) -> None:
        self.updated_at = time.time()

    def close_tunnel(self) -> None:
        """Libère le tunnel et déclenche la période de cooldown post-formulaire."""
        self.active_goal = ""
        self.active_form = None
        self.tunnel_locked = False
        self.metadata["just_finished_action"] = time.time()
        self.mark_dirty()

    def clear_post_form_cooldown(self) -> None:
        self.metadata.pop("just_finished_action", None)
        self.mark_dirty()

    def mark_dirty(self) -> None:
        self._is_dirty = True

    def reset_dirty(self) -> None:
        self._is_dirty = False

    @property
    def is_dirty(self) -> bool:
        return self._is_dirty
