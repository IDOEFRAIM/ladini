"""ContextGuard — empêche l'exécution d'outils hors agent actif."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


class ContextGuard:
    """Vérifie que l'agent appelant correspond à l'agent actif/locké."""

    @staticmethod
    def ensure_agent(state: Dict[str, Any], expected_agent: str = "market") -> Tuple[bool, Optional[str]]:
        active = state.get("locked_agent") or state.get("workspace_agent") or state.get("active_agent")
        if active and active != "market":
            # force workspace back to MarketCoach upstream, but keep a friendly message just in case
            msg = "Je suis en mode MarketCoach. Terminez la tâche en cours avant de poser une autre question."
            return False, msg
        return True, None
