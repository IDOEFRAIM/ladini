"""MarketCoach graph wiring and compatibility adapter."""

from typing import Any, Dict, Optional
import logging

from agriconnect.graphs.agents.common.output import AgriAgentOutput, ExpertMetadata

logger = logging.getLogger("Agent.MarketCoach.graph")

from .nodes import build
from .state import MarketAgentState

_cache: Dict[str, Any] = {}


def _cache_key(llm_client=None, mcp_session=None, checkpointer=None) -> str:
    return f"llm:{repr(llm_client)}|mcp:{repr(mcp_session)}|cp:{repr(checkpointer)}"


def compile_graph(llm_client=None, mcp_session=None, checkpointer=None):
    """Compile and return the MarketCoach sub-graph with injected runtime."""
    # If a checkpointer was explicitly provided, pass it through.
    if checkpointer is not None:
        return build(checkpointer=checkpointer, llm_client=llm_client, mcp_session=mcp_session)

    # Otherwise, try to auto-create a PostgresSaver from mcp_session/context
    saver = None
    try:
        try:
            from langgraph.checkpointer.postgres import PostgresSaver
        except Exception:
            try:
                from langgraph.checkpointer import PostgresSaver
            except Exception:
                PostgresSaver = None

        if PostgresSaver is not None and mcp_session is not None:
            db = getattr(mcp_session, "db", None)
            if db is not None:
                try:
                    conn = getattr(db, "engine", db)
                except Exception:
                    conn = db
                try:
                    saver = PostgresSaver(conn)
                except Exception as exc:
                    logger.warning("Failed to initialize PostgresSaver: %s", exc)
    except Exception as exc:
        logger.debug("PostgresSaver detection error: %s", exc)

    return build(checkpointer=saver, llm_client=llm_client, mcp_session=mcp_session)


def get_agent_graph(llm_client=None, mcp_session=None, checkpointer=None):
    key = _cache_key(llm_client=llm_client, mcp_session=mcp_session, checkpointer=checkpointer)
    if key not in _cache:
        _cache[key] = compile_graph(llm_client=llm_client, mcp_session=mcp_session, checkpointer=checkpointer)
    return _cache[key]


class MarketCoach:
    """Thin adapter for backward compatibility with orchestrator.

    The heavy logic lives in `nodes.build()` which returns a compiled graph.
    """

    def __init__(self, app: Any):
        self._app = app

    @classmethod
    def from_config(cls, llm_client=None, mcp_session=None, checkpointer=None, **overrides: Any):
        return cls(get_agent_graph(llm_client=llm_client, mcp_session=mcp_session, checkpointer=checkpointer))

    def handle(self, query: str, context: Dict[str, Any]) -> Dict[str, Any]:
        initial_state: MarketAgentState = {"user_query": query, "user_profile": context}
        final = self._app.invoke(initial_state)
        output = AgriAgentOutput(
            full_text=final.get("final_response", ""),
            structured_data=final.get("market_data") or {},
            handoff=final.get("handoff_to"),
            expert_metadata=ExpertMetadata(
                name="market_coach",
                confidence=float(final.get("confidence_score") or 0.0),
                sources=final.get("sources") or [],
            ),
        )
        return output.model_dump()

    async def run(self, query: str, context: Dict[str, Any]) -> Dict[str, Any]:
        initial_state: MarketAgentState = {"user_query": query, "user_profile": context}
        final = await self._app.ainvoke(initial_state)
        output = AgriAgentOutput(
            full_text=final.get("final_response", ""),
            structured_data=final.get("market_data") or {},
            handoff=final.get("handoff_to"),
            expert_metadata=ExpertMetadata(
                name="market_coach",
                confidence=float(final.get("confidence_score") or 0.0),
                sources=final.get("sources") or [],
            ),
        )
        return output.model_dump()


__all__ = ["compile_graph", "get_agent_graph", "MarketCoach"]
