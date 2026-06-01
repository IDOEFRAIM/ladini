"""MarketCoach graph wiring.

This module is kept as a stable, legacy import path across the codebase.
The actual DI/caching logic lives in `adapter.py`.
"""

from __future__ import annotations

from agriconnect.graphs.agents.market_coach.adapter import MarketCoach, get_agent_graph

__all__ = ["get_agent_graph", "MarketCoach"]
