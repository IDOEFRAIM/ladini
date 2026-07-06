"""WorkspaceResolver — mono-agent (MarketCoach) state machine.

Legacy routing (Market vs Formation) has been removed. We still load the
_workspace_ from Postgres, keep existing tunnel information, but we always
force the agent to ``"market"``. This keeps the persistence model unchanged
while dramatically simplifying orchestration.
"""
from __future__ import annotations

import logging
from typing import Optional

from agriconnect.workspace.models import Workspace
from agriconnect.workspace.store import WorkspaceStore

logger = logging.getLogger("AgriConnect.Workspace.Resolver")


class WorkspaceResolver:
    """Load or create a workspace and guarantee the MarketCoach agent."""

    def __init__(self, store: Optional[WorkspaceStore] = None):
        self.store = store or WorkspaceStore()

    async def resolve(
        self,
        workspace_id: str,
        user_query: str,  # Kept for API compatibility (unused).
        workspace_type: Optional[str] = None,
    ) -> Workspace:
        ws = await self.store.get(workspace_id)
        if ws is None:
            ws = self._build_initial_workspace(workspace_id, workspace_type)
            logger.info("New workspace %s → agent=%s type=%s", workspace_id, ws.active_agent, ws.workspace_type)
            return ws

        if workspace_type and ws.workspace_type != workspace_type:
            ws.workspace_type = workspace_type

        if ws.active_agent != "market":
            logger.info("Workspace %s forced to MarketCoach agent (was %s)", workspace_id, ws.active_agent)
            ws.active_agent = "market"

        if self._has_active_tunnel(ws):
            ws.locked_agent = ws.locked_agent or ws.active_agent
            logger.info("Workspace %s tunnel actif → locked_agent=%s", workspace_id, ws.locked_agent)
        else:
            ws.locked_agent = None

        # router_clarification is no longer produced, but we clear any stale value.
        ws.metadata.pop("router_clarification", None)
        return ws

    @staticmethod
    def _has_active_tunnel(ws: Workspace) -> bool:
        return bool(ws.active_form) or bool(ws.active_goal)

    @staticmethod
    def _build_initial_workspace(
        workspace_id: str,
        workspace_type: Optional[str],
    ) -> Workspace:
        return Workspace(
            workspace_id=workspace_id,
            active_agent="market",
            workspace_type=(workspace_type or "producer"),
        )
