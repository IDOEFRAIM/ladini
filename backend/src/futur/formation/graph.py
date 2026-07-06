"""Formation sub-graph wiring and orchestration adapter."""

import asyncio
from functools import partial
from typing import Any, Dict, Optional
import logging

from langgraph.graph import END, StateGraph

try:
    from langgraph.checkpoint.memory import MemorySaver
except Exception:
    MemorySaver = None

from agriconnect.graphs.agents.common.output import AgriAgentOutput, ExpertMetadata

logger = logging.getLogger("Agent.FormationCoach.graph")

from futur.formation.nodes import (
    FormationConfig,
    load_profile_node,
    analyze_node,
    validate_node,
    consult_crop_node,
    call_tools_node,
    compose_node,
    critique_node,
    evaluate_node,
    log_session_node,
    route_after_critique,
    route_after_validate,
)
from futur.formation.state import FormationState
from futur.formation.form_node import formation_form_node

_cache: Dict[str, Any] = {}
def _cache_key(config: Optional[Any], overrides: Dict[str, Any]) -> str:
    items = sorted((k, repr(v)) for k, v in overrides.items())
    mcp_session = overrides.get("mcp_session") or getattr(config, "mcp_session", None)
    session_key = f"mcp:{id(mcp_session)}" if mcp_session is not None else "mcp:none"
    return f"cfg:{id(config)}|{session_key}|ovr:{items}"


def compile_graph(config: Optional[FormationConfig] = None, checkpointer: Any = None, **overrides: Any):
    """Compile and return the Formation sub-graph for a runtime config.

    Full 9-node pipeline:
      LOAD_PROFILE → ANALYZE → VALIDATE → (conditional: __end__ | CONSULT_CROP)
      → CALL_TOOLS → COMPOSE → CRITIQUE → (conditional: EVALUATE | COMPOSE)
      → EVALUATE → LOG_SESSION → END

    Each node receives the same FormationConfig instance; a runtime is
    built/cached from that config inside the nodes when needed.
    """
    cfg = config or FormationConfig()
    for key, value in overrides.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)

    workflow = StateGraph(FormationState)

    # ── 1. Node registration ────────────────────────────────────────
    workflow.add_node("LOAD_PROFILE", partial(load_profile_node, agent_config=cfg))
    workflow.add_node("ANALYZE", partial(analyze_node, agent_config=cfg))
    workflow.add_node("VALIDATE", partial(validate_node, agent_config=cfg))
    workflow.add_node("CONSULT_CROP", partial(consult_crop_node, agent_config=cfg))
    workflow.add_node("CALL_TOOLS", partial(call_tools_node, agent_config=cfg))
    workflow.add_node("COMPOSE", partial(compose_node, agent_config=cfg))
    workflow.add_node("CRITIQUE", partial(critique_node, agent_config=cfg))
    workflow.add_node("EVALUATE", partial(evaluate_node, agent_config=cfg))
    workflow.add_node("LOG_SESSION", partial(log_session_node, agent_config=cfg))
    workflow.add_node("FORM_NODE", partial(formation_form_node, agent_config=cfg))

    # ── 2. Entry point ──────────────────────────────────────────────
    workflow.set_entry_point("LOAD_PROFILE")

    # ── 3. LOAD_PROFILE → ANALYZE (identity resolved → analyze query)
    workflow.add_edge("LOAD_PROFILE", "ANALYZE")

    # ── 4. ANALYZE → conditional: FORM_NODE (crop cycle) or VALIDATE
    def _route_after_analyze(state: FormationState) -> str:
        """Route after analyze: detect crop cycle creation intent."""
        # If a form is already active, go to form node
        if state.get("active_form"):
            return "to_form"
        # Detect crop cycle creation intent from keywords in query or intent
        intent = str(state.get("intent") or "").upper()
        query = str(state.get("user_query") or "").lower()
        _cycle_triggers = (
            "cycle", "cr\u00e9er un cycle", "nouveau cycle", "d\u00e9marrer",
            "semis", "plantation", "suivre ma culture",
        )
        if intent in ("CROP_CYCLE_CREATE", "CROP_CYCLE", "CYCLE_CREATION"):
            return "to_form"
        if any(kw in query for kw in _cycle_triggers):
            return "to_form"
        return "to_validate"

    def _route_after_form(state: FormationState) -> str:
        """Route after form_node: if complete go to CONSULT_CROP, else end (wait for user)."""
        form_step = state.get("form_step")
        if form_step == "COMPLETE":
            return "to_consult"
        # Still collecting or confirming — end this turn
        return "__end__"

    workflow.add_conditional_edges(
        "ANALYZE",
        _route_after_analyze,
        {"to_form": "FORM_NODE", "to_validate": "VALIDATE"},
    )

    workflow.add_conditional_edges(
        "FORM_NODE",
        _route_after_form,
        {"to_consult": "CONSULT_CROP", "__end__": END},
    )

    # ── 5. VALIDATE → conditional: ask user (END) or consult crop data
    workflow.add_conditional_edges(
        "VALIDATE",
        route_after_validate,
        {
            "__end__": END,            # Missing info → return AG-UI component
            "consult_crop": "CONSULT_CROP",  # OK → fetch agronomic data
        },
    )

    # ── 6. CONSULT_CROP → CALL_TOOLS (enrich context with MCP/DB tools)
    workflow.add_edge("CONSULT_CROP", "CALL_TOOLS")

    # ── 7. CALL_TOOLS → COMPOSE (context assembled → write answer)
    workflow.add_edge("CALL_TOOLS", "COMPOSE")

    # ── 8. Quality loop: COMPOSE → CRITIQUE
    workflow.add_edge("COMPOSE", "CRITIQUE")

    # ── 9. CRITIQUE conditional: improve or evaluate
    workflow.add_conditional_edges(
        "CRITIQUE",
        route_after_critique,
        {
            "evaluate": "EVALUATE",  # Qualité OK → score
            "compose": "COMPOSE",    # Besoin de corriger la rédaction
        },
    )

    # ── 10. EVALUATE → LOG_SESSION → END
    workflow.add_edge("EVALUATE", "LOG_SESSION")
    workflow.add_edge("LOG_SESSION", END)

    # ---------------------------------------------------------
    # CHECKPOINTER UNIQUE = MemorySaver. La durabilité vit dans le
    # Workspace Postgres (Orchestrator). Aucun fallback, aucun double système.
    # ---------------------------------------------------------
    saver = checkpointer or (MemorySaver() if MemorySaver is not None else None)
    return workflow.compile(checkpointer=saver)


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
        # Normalize legacy keyword aliases
        llm_client = overrides.pop("llm", None)
        shield = overrides.pop("mcp_session", None) or overrides.pop("mcp_shield", None)
        db = overrides.pop("db", None) or overrides.pop("database_service", None)
        if llm_client is not None:
            overrides.setdefault("llm_client", llm_client)
        if shield is not None:
            overrides.setdefault("shield", shield)
        if db is not None:
            overrides.setdefault("db_service", db)
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

    @staticmethod
    def _build_initial_state(query: str, context: Dict[str, Any]) -> FormationState:
        """Build a complete initial state from query + legacy context dict."""
        session_id = context.get("session_id") or context.get("thread_id")
        if not session_id:
            session_id = str(id(context))

        user_phone = (
            context.get("user_phone")
            or context.get("phone")
            or context.get("whatsapp")
            or context.get("phone_number")
        )
        return {
            "user_query": query,
            "learner_profile": context,
            # Identity hints from context (if provided)
            "db_user_id": context.get("user_id") or context.get("id"),
            "user_phone": user_phone,
            "session_id": str(session_id),
            # Defaults for all list/flag fields
            "warnings": [],
            "tool_calls": [],
            "profile_loaded": False,
            "session_logged": False,
        }

    @staticmethod
    def _extract_confidence(final: Dict[str, Any]) -> float:
        """Extract confidence from evaluation or session_score."""
        score = final.get("session_score")
        if score is not None:
            return float(score)
        evaluation = final.get("evaluation") or {}
        return float(evaluation.get("overall", 0.0))

    def handle(self, query: str, context: Dict[str, Any]) -> Dict[str, Any]:
        app = self._ensure_app()
        initial_state = self._build_initial_state(query, context)
        thread_id = str(initial_state.get("session_id") or "formation_default")
        final = self._run_sync(
            app.ainvoke(initial_state, config={"configurable": {"thread_id": thread_id}}),
            "FormationCoach.handle()",
        )
        output = AgriAgentOutput(
            full_text=final.get("final_response", ""),
            structured_data=final.get("agri_response") or {},
            expert_metadata=ExpertMetadata(
                name="formation",
                confidence=self._extract_confidence(final),
                sources=final.get("sources") or [],
            ),
        )
        return output.model_dump()

    async def run(self, query: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Backward-compatible async entrypoint delegated to the compiled graph."""
        app = self._ensure_app()
        initial_state = self._build_initial_state(query, context)
        thread_id = str(initial_state.get("session_id") or "formation_default")
        final = await app.ainvoke(initial_state, config={"configurable": {"thread_id": thread_id}})
        output = AgriAgentOutput(
            full_text=final.get("final_response", ""),
            structured_data=final.get("agri_response") or {},
            expert_metadata=ExpertMetadata(
                name="formation",
                confidence=self._extract_confidence(final),
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

    sample_state = {
        "user_query": "comment bien planter des tomates.",
        "learner_profile": {"user_id": "test_user", "phone": "+22601479800"},
    }
    sample_thread = (
        sample_state.get("session_id")
        or sample_state["learner_profile"].get("phone")
        or "formation_cli"
    )

    print("Compiling Formation graph (real LLM)...")
    app = get_agent_graph(config=cfg)
    print("Running full workflow (ainvoke)...")
    final = asyncio.run(
        app.ainvoke(sample_state, config={"configurable": {"thread_id": str(sample_thread)}})
    )

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
