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
        workspace_type: Optional[str] = None,
    ) -> Workspace:
        ws = await self.store.get(workspace_id)
        if ws is None:
            ws = self._build_initial_workspace(workspace_id, workspace_type)
            logger.info(
                "WorkspaceResolver: new workspace %s type=%s",
                workspace_id, ws.workspace_type,
            )
            return ws

        if workspace_type and ws.workspace_type != workspace_type:
            logger.info(
                "WorkspaceResolver: %s type updated %s → %s",
                workspace_id, ws.workspace_type, workspace_type,
            )
            ws.workspace_type = workspace_type
            ws.mark_dirty()

        has_tunnel = self._has_active_tunnel(ws)
        ws.tunnel_locked = has_tunnel
        if has_tunnel:
            logger.info(
                "WorkspaceResolver: %s tunnel actif goal=%r form=%r",
                workspace_id,
                ws.active_goal,
                ws.active_form,
            )

        # router_clarification is no longer produced, but we clear any stale value.
        if ws.metadata.pop("router_clarification", None) is not None:
            ws.mark_dirty()

        logger.debug("WorkspaceResolver: resolved %r", ws)
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
            workspace_type=(workspace_type or "producer"),
        )
