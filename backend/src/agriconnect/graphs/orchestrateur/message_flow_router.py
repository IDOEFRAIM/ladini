"""Routing helper extracted from message_flow to reduce cyclomatic complexity.

Provides MessageRouter.route_flow(state) which implements routing decisions.
"""
from typing import Literal, Dict, Any


class MessageRouter:
    @staticmethod
    def route_flow(state: Dict[str, Any]) -> Literal[
        "EXECUTE_CHAT",
        "SOLO_SENTINELLE",
        "SOLO_FORMATION",
        "SOLO_MARKET",
        "SOLO_MARKETPLACE",
        "PARALLEL_EXPERTS",
        "REJECT",
    ]:
        """Determine execution path from analysis payload.

        This mirrors the previous logic but is isolated for testability and
        to reduce the complexity inside MessageResponseFlow.
        """
        analysis = state.get("needs", {})
        intent = analysis.get("intent", "COUNCIL")

        if intent == "REJECT":
            return "REJECT"
        if intent == "CHAT":
            return "EXECUTE_CHAT"

        s = analysis.get("needs_sentinelle", False)
        f = analysis.get("needs_formation", False)
        m = analysis.get("needs_market", False)
        mp = analysis.get("needs_marketplace", False)

        active = sum(1 for x in (s, f, m, mp) if x)

        # Marketplace is prioritized when alone
        if mp and active == 1:
            return "SOLO_MARKETPLACE"

        if intent == "COUNCIL" or active > 1:
            return "PARALLEL_EXPERTS"

        if s:
            return "SOLO_SENTINELLE"
        if f:
            return "SOLO_FORMATION"
        if m:
            return "SOLO_MARKET"

        return "PARALLEL_EXPERTS"
