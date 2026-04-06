"""Formation sub-graph wiring and orchestration adapter."""

from functools import partial
from typing import Any, Dict, Optional
import logging

from langgraph.graph import END, StateGraph

from agriconnect.graphs.agents.common.output import AgriAgentOutput, ExpertMetadata

logger = logging.getLogger("Agent.FormationCoach.graph")

from .nodes import (
    FormationConfig,
    analyze_query_node,
    audit_node,
    finalize_node,
    generate_answer_node,
    retrieve_node,
)
from .state import FormationState

_cache: Dict[str, Any] = {}


def _cache_key(config: Optional[Any], overrides: Dict[str, Any]) -> str:
    items = sorted((k, repr(v)) for k, v in overrides.items())
    return f"cfg:{id(config)}|ovr:{items}"


def compile_graph(config: Optional[FormationConfig] = None, **overrides: Any):
    """Compile and return the Formation sub-graph for a runtime config.

    The compiled graph wires pure node functions (no `self` or internal run() calls).
    Each node receives the same `FormationConfig` instance; a runtime is built/cached
    from that config inside the nodes when needed.
    """
    cfg = config or FormationConfig()
    for key, value in overrides.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)

    workflow = StateGraph(FormationState)
    workflow.add_node("ANALYZE", partial(analyze_query_node, agent_config=cfg))
    workflow.add_node("RETRIEVE", partial(retrieve_node, agent_config=cfg))
    workflow.add_node("GENERATE", partial(generate_answer_node, agent_config=cfg))
    workflow.add_node("FINALIZE", partial(finalize_node, agent_config=cfg))
    workflow.add_node("AUDIT", partial(audit_node, agent_config=cfg))
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
                # alternative import path
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


def get_agent_graph(config: Optional[FormationConfig] = None, **overrides: Any):
    key = _cache_key(config, overrides)
    if key not in _cache:
        _cache[key] = compile_graph(config=config, **overrides)
    return _cache[key]


class FormationCoach:
    """Thin adapter for legacy orchestrator integration.

    Keeps backward-compatible `handle` / `run` methods but delegates to the
    StateGraph compiled above. This adapter is intentionally small so the
    heavy logic remains in pure node functions in `nodes.py`.
    """

    def __init__(self, app: Any):
        self._app = app

    @classmethod
    def from_config(cls, config: Optional[FormationConfig] = None, **overrides: Any):
        cfg = config or FormationConfig()
        llm_client = overrides.pop("llm", None)
        shield = overrides.pop("mcp_session", None) or overrides.pop("mcp_shield", None)
        if llm_client is not None:
            overrides.setdefault("llm_client", llm_client)
        if shield is not None:
            overrides.setdefault("shield", shield)
        for key, value in overrides.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        return cls(get_agent_graph(config=cfg))

    def handle(self, query: str, context: Dict[str, Any]) -> Dict[str, Any]:
        initial_state: FormationState = {
            "user_query": query,
            "learner_profile": context,
        }
        final = self._app.invoke(initial_state)
        output = AgriAgentOutput(
            full_text=final.get("final_response", ""),
            structured_data=final.get("agri_response") or {},
            handoff=final.get("required_domain") or final.get("handoff_to"),
            expert_metadata=ExpertMetadata(
                name="formation",
                confidence=float(final.get("confidence_score") or 0.0),
                sources=final.get("sources") or [],
            ),
        )
        return output.model_dump()

    async def run(self, query: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Backward-compatible async entrypoint delegated to the compiled graph."""
        initial_state: FormationState = {
            "user_query": query,
            "learner_profile": context,
        }
        final = await self._app.ainvoke(initial_state)
        output = AgriAgentOutput(
            full_text=final.get("final_response", ""),
            structured_data=final.get("agri_response") or {},
            handoff=final.get("required_domain") or final.get("handoff_to"),
            expert_metadata=ExpertMetadata(
                name="formation",
                confidence=float(final.get("confidence_score") or 0.0),
                sources=final.get("sources") or [],
            ),
        )
        return output.model_dump()


__all__ = ["compile_graph", "get_agent_graph", "FormationCoach", "FormationConfig"]


if __name__ == "__main__":
    """Run the Formation graph end-to-end using the real configured LLM.

    WARNING: this will import/initialize real LLM and langgraph/langchain
    dependencies and may perform network calls and incur costs. Ensure your
    API keys and environment are configured before running.
    """
    import asyncio
    import json
    import os

    cfg = FormationConfig()
    # Allow overriding model names via environment for quick experimentation
    if os.environ.get("FORMATION_MODEL_PLANNER"):
        cfg.model_planner = os.environ.get("FORMATION_MODEL_PLANNER")
    if os.environ.get("FORMATION_MODEL_ANSWER"):
        cfg.model_answer = os.environ.get("FORMATION_MODEL_ANSWER")

    # Note: Formation requires a configured `shield`/MCP session in production.
    # For local dev runs you can set the env var FORMATION_LOCAL_NO_SHIELD=1
    # to bypass this check and use a dummy shield object.
    if os.environ.get("FORMATION_LOCAL_NO_SHIELD"):
        cfg.shield = object()

    sample_state = {"user_query": "Bonjour, je veux des conseils sur le semis du maïs.", "learner_profile": {"user_id": "test_user"}}

    print("Compiling Formation graph (real LLM)...")
    app = get_agent_graph(config=cfg)
    print("Running full workflow (invoke)...")
    try:
        final = app.invoke(sample_state)
    except Exception:
        # fallback to async invocation if only async interface is available
        final = asyncio.run(app.ainvoke(sample_state))

    print("Final state:")
    print(json.dumps(final, ensure_ascii=False, indent=2))

    out = AgriAgentOutput(
        full_text=final.get("final_response", ""),
        structured_data=final.get("agri_response") or {},
        handoff=final.get("required_domain") or final.get("handoff_to"),
        expert_metadata=ExpertMetadata(
            name="formation",
            confidence=float(final.get("confidence_score") or 0.0),
            sources=final.get("sources") or [],
        ),
    )
    print("\nStandardized output:\n", json.dumps(out.model_dump(), ensure_ascii=False, indent=2))
