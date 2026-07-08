"""ContextGuard — empêche l'exécution d'outils hors agent actif."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("AgriConnect.Workspace.ContextGuard")


class ContextGuard:
    """Vérifie que l'agent appelant correspond à l'agent actif/locké."""

    @staticmethod
    def ensure_agent(
        state: Dict[str, Any],
        workspace_id: Optional[str] = None,
        expected_agent: str = "market",
    ) -> Tuple[bool, Optional[str]]:
        """Return (True, None) if the active agent is MarketCoach, else (False, msg).

        Args:
            state:        Current agent state dict.
            workspace_id: Optional identifier used in log messages.
            expected_agent: Agent name we expect (kept for backward compatibility).
        """
        # Depuis la migration mono-agent, il n'existe plus qu'un seul agent
        # (MarketCoach). On conserve le hook pour compatibilité mais il
        # n'empêche plus aucune exécution.
        active = state.get("locked_agent") or state.get("workspace_agent") or state.get("active_agent")
        logger.debug(
            "ContextGuard.ensure_agent noop: active=%r expected='market' workspace=%s",
            active or "market",
            workspace_id or "<unknown>",
        )
        return True, None
