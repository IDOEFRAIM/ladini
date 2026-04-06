"""Sentinelle graph wiring and adapter."""

from functools import partial
from typing import Any, Dict, Optional
import logging

from langgraph.graph import END, StateGraph

from agriconnect.graphs.agents.common.output import AgriAgentOutput, ExpertMetadata

logger = logging.getLogger("Agent.Sentinelle.graph")

from .nodes import (
    SentinelConfig,
    analyze_node,
    audit_node,
    build_runtime,
    finalize_node,
    generate_node,
    retrieve_node,
)
from .state import SentinelState

_cache: Dict[str, Any] = {}


def _cache_key(config: Optional[Any], overrides: Dict[str, Any]) -> str:
    items = sorted((k, repr(v)) for k, v in overrides.items())
    return f"cfg:{id(config)}|ovr:{items}"


def compile_graph(config: Optional[SentinelConfig] = None, **overrides: Any):
    runtime = build_runtime(config=config, **overrides)
    workflow = StateGraph(SentinelState)
    workflow.add_node("ANALYZE", partial(analyze_node, runtime=runtime))
    workflow.add_node("RETRIEVE", partial(retrieve_node, runtime=runtime))
    workflow.add_node("GENERATE", partial(generate_node, runtime=runtime))
    workflow.add_node("FINALIZE", partial(finalize_node, runtime=runtime))
    workflow.add_node("AUDIT", partial(audit_node, runtime=runtime))
    workflow.set_entry_point("ANALYZE")
    workflow.add_edge("ANALYZE", "RETRIEVE")
    workflow.add_edge("RETRIEVE", "GENERATE")
    workflow.add_edge("GENERATE", "FINALIZE")
    workflow.add_edge("FINALIZE", "AUDIT")
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

        if PostgresSaver is not None and hasattr(runtime, "mcp_context") and getattr(runtime, "mcp_context") is not None:
            db = getattr(runtime.mcp_context, "db", None)
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


def get_agent_graph(config: Optional[SentinelConfig] = None, **overrides: Any):
    key = _cache_key(config, overrides)
    if key not in _cache:
        _cache[key] = compile_graph(config=config, **overrides)
    return _cache[key]


class ClimateSentinel:
    def __init__(self, app: Any):
        self._app = app

    @classmethod
    def from_config(cls, config: Optional[SentinelConfig] = None, **overrides: Any):
        return cls(get_agent_graph(config=config, **overrides))

    def handle(self, query: str, context: Dict[str, Any]) -> Dict[str, Any]:
        initial_state: SentinelState = {"user_query": query, "location_profile": context}
        final = self._app.invoke(initial_state)
        output = AgriAgentOutput(
            full_text=final.get("final_response", ""),
            structured_data=final.get("agri_response") or {},
            handoff=final.get("handoff_to"),
            expert_metadata=ExpertMetadata(
                name="sentinelle",
                confidence=float(final.get("confidence_score") or 0.0),
                sources=final.get("sources") or [],
            ),
        )
        return output.model_dump()

    async def run(self, query: str, context: Dict[str, Any]) -> Dict[str, Any]:
        initial_state: SentinelState = {"user_query": query, "location_profile": context}
        final = await self._app.ainvoke(initial_state)
        output = AgriAgentOutput(
            full_text=final.get("final_response", ""),
            structured_data=final.get("agri_response") or {},
            handoff=final.get("handoff_to"),
            expert_metadata=ExpertMetadata(
                name="sentinelle",
                confidence=float(final.get("confidence_score") or 0.0),
                sources=final.get("sources") or [],
            ),
        )
        return output.model_dump()


__all__ = ["compile_graph", "get_agent_graph", "ClimateSentinel", "SentinelConfig"]


if __name__ == "__main__":
    """Run the Sentinelle graph end-to-end using the real configured LLM.

    WARNING: this will import/initialize real LLM and langgraph/langchain
    dependencies and may perform network calls. Ensure your environment is
    configured with the proper API keys and resources.
    """
    import asyncio
    import json
    import os

    cfg = SentinelConfig()
    # Do not inject a mock LLM: let build_runtime resolve the real client
    # Optionally pick up environment overrides
    if os.environ.get("SENTINEL_MODEL_PLANNER"):
        cfg.model_planner = os.environ.get("SENTINEL_MODEL_PLANNER")
    if os.environ.get("SENTINEL_MODEL_ANSWER"):
        cfg.model_answer = os.environ.get("SENTINEL_MODEL_ANSWER")

    sample_state = {
        "user_query": "Mon champ de maïs montre des taches brunes, que faire ?",
        "location_profile": {"zone": "TestZone", "user_id": "test_user"},
    }

    print("Compiling Sentinelle graph (real LLM)...")
    app = get_agent_graph(config=cfg)
    print("Running full workflow (invoke)...")
    try:
        final = app.invoke(sample_state)
    except Exception:
        final = asyncio.run(app.ainvoke(sample_state))

    print("Final state:")
    print(json.dumps(final, ensure_ascii=False, indent=2))

    out = AgriAgentOutput(
        full_text=final.get("final_response", ""),
        structured_data=final.get("agri_response") or {},
        handoff=final.get("handoff_to"),
        expert_metadata=ExpertMetadata(
            name="sentinelle",
            confidence=float(final.get("confidence_score") or 0.0),
            sources=final.get("sources") or [],
        ),
    )
    print("\nStandardized output:\n", json.dumps(out.model_dump(), ensure_ascii=False, indent=2))
