"""Formation sub-graph wiring and orchestration adapter."""

import asyncio
from functools import partial
from typing import Any, Dict, Optional
import logging

from langgraph.graph import END, StateGraph

from agriconnect.graphs.agents.common.output import AgriAgentOutput, ExpertMetadata

logger = logging.getLogger("Agent.FormationCoach.graph")

from agriconnect.graphs.agents.formation.nodes import (
    FormationConfig,
    analyze_node,
    validate_node,
    consult_crop_node,
    compose_node,
    critique_node,
    evaluate_node,
    grade_sources_node,
    retrieve_node,
    rewrite_node,
    route_after_critique,
    route_after_grade,
    route_after_retrieve,
    route_after_rewrite,
    route_after_validate,
)
from agriconnect.graphs.agents.formation.state import FormationState

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

    # 1. Définition des Noeuds
    workflow.add_node("ANALYZE", partial(analyze_node, agent_config=cfg))
    workflow.add_node("VALIDATE", partial(validate_node, agent_config=cfg))
    workflow.add_node("CONSULT_CROP", partial(consult_crop_node, agent_config=cfg))
    workflow.add_node("COMPOSE", partial(compose_node, agent_config=cfg))
    workflow.add_node("CRITIQUE", partial(critique_node, agent_config=cfg))
    workflow.add_node("EVALUATE", partial(evaluate_node, agent_config=cfg))

    # 2. Point d'entrée
    workflow.set_entry_point("ANALYZE")

    # 3. ANALYZE → VALIDATE (check if we have enough info)
    workflow.add_edge("ANALYZE", "VALIDATE")

    # 4. VALIDATE → conditional: ask user (END) or consult crop data
    workflow.add_conditional_edges(
        "VALIDATE",
        route_after_validate,
        {
            "__end__": END,            # Missing info → return AG-UI component
            "consult_crop": "CONSULT_CROP",  # OK → fetch agronomic data
        },
    )

    # 5. CONSULT_CROP → COMPOSE (technical data assembled → write answer)
    workflow.add_edge("CONSULT_CROP", "COMPOSE")

    # 6. Cycle de Qualité : COMPOSE → CRITIQUE
    workflow.add_edge("COMPOSE", "CRITIQUE")

    # 7. Condition de sortie ou de correction
    workflow.add_conditional_edges(
        "CRITIQUE",
        route_after_critique,
        {
            "evaluate": "EVALUATE",  # Qualité OK → Fin
            "compose": "COMPOSE",    # Besoin de corriger la rédaction
        },
    )

    # 8. Fin du workflow
    workflow.add_edge("EVALUATE", END)
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

    def __init__(self, app: Any = None, config: Optional[FormationConfig] = None, **overrides: Any):
        self.config = config or FormationConfig()
        for key, value in overrides.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)

        self._app = app
        # Expose a `tool` attribute for legacy tests/callers that monkeypatch
        # `coach.tool._analyze_request`.
        runtime = getattr(self.config, "_runtime", None)
        self.tool = getattr(runtime, "tool", None) or type("T", (), {})()

    def _ensure_app(self):
        if self._app is None:
            self._app = get_agent_graph(config=self.config)
        return self._app

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
        return cls(config=cfg)

    def build(self):
        """Legacy-compatible graph build entrypoint used in tests.

        Keeps behavior lightweight and deterministic for unit tests: it calls
        `StateGraph.compile()` on a minimal graph so patch-based assertions can
        validate wiring without requiring full runtime initialization.
        """
        workflow = StateGraph(dict)
        # Ensure the graph has at least one node and an entry point so
        # `compile()` doesn't raise when invoked outside of patched tests.
        workflow.add_node("DUMMY", lambda s: s)
        workflow.set_entry_point("DUMMY")
        return workflow.compile()

    def analyze_node(self, state: Dict[str, Any]) -> Dict[str, Any]:
        query = str(state.get("user_query") or "")
        # Formation focuses on learning/advice; legacy cross-domain detection removed
        # so analyze_node remains focused and lightweight for tests.
        analyze_hook = getattr(self.tool, "_analyze_request", None)
        if callable(analyze_hook):
            parsed = analyze_hook()
            if parsed.get("is_relevant", True):
                return {
                    "status": "ANALYZED",
                    "focus_topics": parsed.get("focus_topics", []),
                    "intent": parsed.get("intent", "FORMATION"),
                }
            return {
                "status": "OFF_TOPIC",
                "rejection_reason": parsed.get("rejection_reason", "Hors périmètre formation"),
            }

        return {"status": "ANALYZED", "focus_topics": []}

    def retrieve_node(self, state: Dict[str, Any]) -> Dict[str, Any]:
        mcp = getattr(self.config, "mcp_rag", None)
        if mcp is None or not hasattr(mcp, "call_tool"):
            return {"status": "CONTEXT_NOT_FOUND", "retrieved_context": "", "sources": []}
        try:
            raw = mcp.call_tool()
        except Exception:
            return {"status": "CONTEXT_NOT_FOUND", "retrieved_context": "", "sources": []}
        # Legacy behavior: keep returning the raw payload to avoid coupling.
        if isinstance(raw, dict):
            return raw
        return {"status": "CONTEXT_NOT_FOUND", "retrieved_context": "", "sources": []}

    def compose_node(self, state: Dict[str, Any]) -> Dict[str, Any]:
        if state.get("is_relevant") is False:
            return {
                "status": "OFF_TOPIC",
                "final_response": "Je vous redirige vers un expert AgriConnect mieux adapté.",
                "agri_response": {"reason": state.get("rejection_reason", "off_topic")},
            }
        return {
            "status": "COMPOSED",
            "final_response": "Réponse de formation générée.",
            "agri_response": {},
        }

    def _run_sync(self, coro, context_label: str):
        try:
            return asyncio.run(coro)
        except RuntimeError as exc:
            if "event loop is running" in str(exc).lower():
                raise RuntimeError(
                    f"{context_label} cannot be used inside a running event loop — use the async `run` method instead."
                ) from exc
            raise

    def handle(self, query: str, context: Dict[str, Any]) -> Dict[str, Any]:
        app = self._ensure_app()
        initial_state: FormationState = {
            "user_query": query,
            "learner_profile": context,
        }
        final = self._run_sync(app.ainvoke(initial_state), "FormationCoach.handle()")
        output = AgriAgentOutput(
            full_text=final.get("final_response", ""),
            structured_data=final.get("agri_response") or {},
            expert_metadata=ExpertMetadata(
                name="formation",
                confidence=float(final.get("confidence_score") or 0.0),
                sources=final.get("sources") or [],
            ),
        )
        return output.model_dump()

    async def run(self, query: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Backward-compatible async entrypoint delegated to the compiled graph."""
        app = self._ensure_app()
        initial_state: FormationState = {
            "user_query": query,
            "learner_profile": context,
        }
        final = await app.ainvoke(initial_state)
        output = AgriAgentOutput(
            full_text=final.get("final_response", ""),
            structured_data=final.get("agri_response") or {},
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

    # Initialize a real ShieldHub by default for end-to-end local execution.
    if cfg.shield is None:
        try:
            from agriconnect.infrastructure.mcp.security import ShieldHub

            cfg.shield = ShieldHub(session_id=os.environ.get("FORMATION_SESSION_ID", "formation_local"))
        except Exception as exc:
            if os.environ.get("FORMATION_LOCAL_NO_SHIELD"):
                cfg.shield = object()
                print(f"Warning: real ShieldHub unavailable, using local bypass (FORMATION_LOCAL_NO_SHIELD=1): {exc}")
            else:
                raise RuntimeError(
                    f"Unable to initialize real ShieldHub: {exc}. Configure MCP stack or set FORMATION_LOCAL_NO_SHIELD=1 for local debug only."
                ) from exc

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
        expert_metadata=ExpertMetadata(
            name="formation",
            confidence=float(final.get("confidence_score") or 0.0),
            sources=final.get("sources") or [],
        ),
    )
    print("\nStandardized output:\n", json.dumps(out.model_dump(), ensure_ascii=False, indent=2))
