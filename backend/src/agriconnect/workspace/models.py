"""Workspace — unité de contexte durable d'AgriConnect.

Toutes les décisions de routage et d'exécution partent du Workspace,
jamais du dernier message seul. Un Workspace par utilisateur (clé = téléphone).

Historique : nous avions plusieurs agents (market vs formation). Le Workspace
désigne désormais **exclusivement** l'agent MarketCoach. Le champ
``active_agent`` est conservé pour compatibilité mais reste forcé à ``"market"``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

VALID_AGENTS = ("market",)


@dataclass(slots=True)
class Workspace:
    """État durable d'une conversation utilisateur.

    Champs:
        workspace_id: identifiant stable (numéro de téléphone normalisé).
        workspace_type: "producer" | "buyer" (rôle marché de l'utilisateur).
        active_agent: agent actif ("market"|"formation").
        active_goal: objectif courant (ex: "SALES_PUBLISH_PRODUCT").
        active_form: formulaire en cours (ex: "PRODUCT_CREATE") ou None.
        locked_agent: agent verrouillé lorsque le tunnel est actif.
        metadata: snapshot d'état agent + mémoire conversationnelle (JSONB).
        updated_at: epoch seconds du dernier write.
    """

    workspace_id: str
    workspace_type: str 
    active_agent: str = "market"
    active_goal: str = ""
    active_form: Optional[str] = None
    locked_agent: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    agent_state: Dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

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
            active_agent=str(data.get("active_agent") or "market"),
            active_goal=str(data.get("active_goal") or ""),
            active_form=data.get("active_form"),
            locked_agent=data.get("locked_agent"),
            metadata=dict(data.get("metadata") or {}),
            agent_state=dict(data.get("langgraph_state") or {}),
            updated_at=float(data.get("updated_at") or time.time()),
        )

    def touch(self) -> None:
        self.updated_at = time.time()

    def close_tunnel(self) -> None:
        """Libère le tunnel et déclenche la période de cooldown post-formulaire."""
        self.active_goal = ""
        self.active_form = None
        self.locked_agent = None
        self.metadata["just_finished_action"] = time.time()

    def clear_post_form_cooldown(self) -> None:
        self.metadata.pop("just_finished_action", None)
