"""Marketplace v3 sub-graph wiring and legacy adapter.

Provides `compile_graph` to build a runtime-bound workflow and a thin
`MarketplaceAgentV3` adapter class kept for backward compatibility with
existing orchestrator consumers.
"""

from functools import partial
from typing import Any, Dict, Optional
import logging

from langgraph.graph import END, StateGraph

from agriconnect.graphs.agents.common.output import AgriAgentOutput, ExpertMetadata

logger = logging.getLogger("Agent.Marketplace.graph")

from .nodes import (
    MarketplaceConfig,
    build_runtime,
    identify_user_node,
    parse_intent_node,
    validate_action_node,
    execute_action_node,
    confirm_node,
    audit_node,
)
from .state import MarketplaceState

_cache: Dict[str, Any] = {}


def _cache_key(config: Optional[Any], overrides: Dict[str, Any]) -> str:
    items = sorted((k, repr(v)) for k, v in overrides.items())
    return f"cfg:{id(config)}|ovr:{items}"


def compile_graph(config: Optional[MarketplaceConfig] = None, **overrides: Any):
    runtime = build_runtime(config=config, **overrides)
    workflow = StateGraph(MarketplaceState)
    workflow.add_node("IDENTIFY", partial(identify_user_node, runtime=runtime))
    workflow.add_node("PARSE", partial(parse_intent_node, runtime=runtime))
    workflow.add_node("VALIDATE", partial(validate_action_node, runtime=runtime))
    workflow.add_node("EXECUTE", partial(execute_action_node, runtime=runtime))
    workflow.add_node("CONFIRM", partial(confirm_node, runtime=runtime))
    workflow.add_node("AUDIT", partial(audit_node, runtime=runtime))
    workflow.set_entry_point("IDENTIFY")
    workflow.add_edge("IDENTIFY", "PARSE")
    workflow.add_edge("PARSE", "VALIDATE")
    workflow.add_edge("VALIDATE", "EXECUTE")
    workflow.add_edge("EXECUTE", "CONFIRM")
    workflow.add_edge("CONFIRM", "AUDIT")
    workflow.add_edge("AUDIT", END)
    # Try to attach a Postgres-backed checkpointer if available and configured
    saver = None
    try:
        try:
            from langgraph.checkpointer.postgres import PostgresSaver
        except Exception:
            try:
                from langgraph.checkpointer import PostgresSaver
            except Exception:
                PostgresSaver = None

        if PostgresSaver is not None and hasattr(cfg, "ctx") and getattr(cfg, "ctx") is not None:
            db = getattr(cfg.ctx, "db", None)
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

    if saver:
        try:
            return workflow.compile(checkpointer=saver)
        except TypeError:
            compiled = workflow.compile()
            try:
                setattr(compiled, "checkpointer", saver)
            except Exception:
                logger.debug("Could not set checkpointer attribute on compiled workflow")
            return compiled

    return workflow.compile()


def get_agent_graph(config: Optional[MarketplaceConfig] = None, **overrides: Any):
    key = _cache_key(config, overrides)
    if key not in _cache:
        _cache[key] = compile_graph(config=config, **overrides)
    return _cache[key]


class MarketplaceAgentV3:
    """Thin adapter for legacy consumers.

    Accepts the same constructor kwargs as the old class (llm_client,
    mcp_session, db_service, ctx, etc.) and exposes `run` and `build_graph`
    compatibility methods.
    """

    def __init__(self, app: Any):
        self._app = app

    @classmethod
    def from_config(cls, config: Optional[MarketplaceConfig] = None, **overrides: Any):
        llm = overrides.pop("llm_client", None) or overrides.pop("llm", None)
        if llm is not None:
            overrides.setdefault("llm_client", llm)
        return cls(get_agent_graph(config=config, **overrides))

    def build_graph(self):
        return self._app

    def run(self, query: str, context: Dict[str, Any]) -> Dict[str, Any]:
        initial_state: MarketplaceState = {
            "user_query": query,
            "user_phone": context.get("user_phone", ""),
            "zone_id": context.get("zone_id"),
        }
        final = self._app.invoke(initial_state)
        output = AgriAgentOutput(
            full_text=final.get("final_response", ""),
            structured_data=final.get("action_result") or {},
            handoff=final.get("handoff_to"),
            expert_metadata=ExpertMetadata(
                name="marketplace_v3",
                confidence=float(final.get("confidence_score") or 0.0),
                sources=final.get("sources") or [],
            ),
        )
        return output.model_dump()


__all__ = ["compile_graph", "get_agent_graph", "MarketplaceAgentV3", "MarketplaceConfig"]
