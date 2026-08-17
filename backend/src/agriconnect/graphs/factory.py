"""Graph factory exposing role-aware MarketCoach builders."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from agriconnect.graphs.agents.market_coach.core.graph_builder import build_graph
from agriconnect.graphs.roles import normalize_role


class GraphFactory:
    """Creates and caches LangGraphs with role isolation."""

    def __init__(self) -> None:
        self._cache: Dict[Tuple[str, int, int], Any] = {}

    @staticmethod
    def _cache_key(
        role: str, mc_runtime: Any, checkpointer: Any
    ) -> Tuple[str, int, int]:
        role_norm = normalize_role(role)
        runtime_id = id(mc_runtime) if mc_runtime is not None else 0
        checkpointer_id = id(checkpointer) if checkpointer is not None else 0
        return role_norm, runtime_id, checkpointer_id

    def get_graph(
        self,
        role: str,
        *,
        mc_runtime: Optional[Any] = None,
        checkpointer: Any = None,
        llm_client: Any = None,
        mcp_session: Any = None,
    ) -> Any:
        role_norm = normalize_role(role)
        cache_key = self._cache_key(role_norm, mc_runtime, checkpointer)

        if cache_key in self._cache:
            return self._cache[cache_key]

        workflow = build_graph(
            role=role_norm,
            mc_runtime=mc_runtime,
            checkpointer=checkpointer,
            llm_client=llm_client,
            mcp_session=mcp_session,
        )

        if mc_runtime is not None and checkpointer is not None:
            self._cache[cache_key] = workflow

        return workflow

    def clear(self) -> None:
        self._cache.clear()


__all__ = ["GraphFactory"]
