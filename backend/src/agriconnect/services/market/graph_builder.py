"""Market — Graph Builder (factory commune Producer / Buyer).

Assemble le StateGraph LangGraph à partir des nœuds partagés (`shared_core`),
de l'interpréteur role-aware (`interpreter_routing`) et du Context Resolver
spécifique au rôle (`producer_flow` ou `buyer_flow`).

Architecture du graphe (14 nœuds) :
    input_normalizer → security_moderation → input_interpreter
    → cognitive_guard → cognitive_orchestrator → clarification_node → semantic_disambiguation
    → goal_planner → memory_update → validator
    → context_resolver → confirmation_gate → mcp_tool_executor
    → response_strategy → final_response → END

Le `clarification_node` est un pass-through silencieux sauf quand l'événement
est OUT_OF_SCOPE/UNKNOWN sans tunnel actif : il génère alors une réponse
pédagogique via LLM et court-circuite vers response_strategy.

Le `semantic_disambiguation` court-circuite vers `response_strategy` QUE si
l'intention LLM est ambiguë ET qu'un déclencheur lexical matche.
"""
from __future__ import annotations

import logging
import sqlite3
from functools import partial
from typing import Any, Optional

from langgraph.graph import END, StateGraph
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.checkpoint.redis import AsyncRedisSaver
import redis.asyncio as redis

from agriconnect.graphs.agents.market_coach.state import MarketAgentState
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    build_runtime,
    build_runtime_from_session,
)

from .buyer_flow import buyer_context_resolver
from .interpreter_routing import (
    goal_planner,
    make_input_interpreter,
    make_route_after_validator,
)
from .producer_flow import producer_context_resolver
from .shared_core import (
    _route_after_confirmation,
    _route_after_executor,
    _route_after_planner,
    _route_after_resolver,
    _route_after_security,
    _safe_node,
    clarification_node,
    cognitive_orchestrator,
    confirmation_gate,
    cognitive_guard,
    final_response,
    input_normalizer,
    mcp_tool_executor,
    memory_update,
    onboarding_node,
    response_strategy,
    security_moderation,
    semantic_disambiguation,
    validator,
)

logger = logging.getLogger("AgriConnect.Market.GraphBuilder")


def _route_after_clarification(state: MarketAgentState) -> str:
    """Routage post-clarification.

    Court-circuite vers response_strategy si :
    - Le clarification_node a généré une réponse pédagogique (CLARIFICATION + final_response)
    - Le cognitive_guard a décidé RECOVERY ou CLARIFICATION avec tunnel abandonné
    Sinon : continue vers semantic_disambiguation.
    """
    strategy = str(state.get("response_strategy") or "").upper()
    # Clarification or Recovery already decided — short-circuit to response
    if strategy in {"CLARIFICATION", "RECOVERY"} and state.get("final_response"):
        return "to_strategy"
    # Cognitive guard set RECOVERY without final_response — still short-circuit
    if strategy == "RECOVERY":
        return "to_strategy"
    return "to_disambiguation"


def _route_after_disambiguation(state: MarketAgentState) -> str:
    """Routage post-désambiguïsation.

    Si le nœud a déclenché un menu (`current_goal == DISAMBIGUATION_PENDING`),
    on saute le goal_planner et on rend la réponse à l'utilisateur. Sinon le
    nœud était un pass-through silencieux et on continue le flow nominal.
    """
    if state.get("current_goal") == "DISAMBIGUATION_PENDING" and state.get("expected_input") == "SELECTION":
        return "to_strategy"
    return "to_planner"


def _route_after_cognitive(state: MarketAgentState) -> str:
    """Route prioritaire vers le nœud d'onboarding lorsque nécessaire."""
    if state.get("is_onboarding"):
        return "to_onboarding"
    return "to_clarification"


def build_graph(
    role: str = "PRODUCER",
    mc_runtime: Optional[MarketRuntime] = None,
    checkpointer: Any = None,
    llm_client: Any = None,
    mcp_session: Any = None,
):
    """Compile un StateGraph LangGraph spécialisé pour un rôle utilisateur (AG-UI).

    Args:
        role: "PRODUCER" (par défaut) ou "BUYER".
        mc_runtime: instance MarketRuntime déjà construite. Prioritaire si fourni.
        checkpointer: checkpointer LangGraph (de préférence AsyncSqliteSaver en prod).
        llm_client: client LLM (Groq, OpenAI...) si mc_runtime n'est pas fourni.
        mcp_session: session MCP partagée fournie par l'orchestrateur.

    Returns:
        L'application LangGraph compilée et prête à `ainvoke()`.
    """
    role_up = str(role or "PRODUCER").upper().strip()
    if role_up not in {"PRODUCER", "BUYER"}:
        logger.warning("Role inconnu '%s' — fallback PRODUCER", role)
        role_up = "PRODUCER"

    if mc_runtime is None:
        if mcp_session is not None:
            mc_runtime = build_runtime_from_session(llm_client=llm_client, mcp_session=mcp_session)
        else:
            mc_runtime = build_runtime(llm_client=llm_client)

    # ---------------------------------------------------------
    # GESTION DE LA MÉMOIRE (CHECKPOINTER)
    # ---------------------------------------------------------
    if checkpointer is None:
        # Fallback de secours : si aucun checkpointer n'est fourni, on utilise 
        # la version synchrone de SqliteSaver pour ne pas bloquer les tests locaux.
        # En production, Celery DOIT injecter son AsyncSqliteSaver.
        logger.info("Aucun checkpointer fourni. Initialisation d'un SqliteSaver (synchrone) de fallback.")
        db_connection = sqlite3.connect("agriconnect_memory.db", check_same_thread=False)
        checkpointer = SqliteSaver(db_connection)
    else:
        logger.info("Checkpointer injecté avec succès : %s", type(checkpointer).__name__)

    # Nœuds et routes spécialisés AG-UI
    input_interpreter = make_input_interpreter(role_up)
    context_resolver = (
        buyer_context_resolver if role_up == "BUYER" else producer_context_resolver
    )
    route_after_validator = make_route_after_validator(role_up)

    workflow = StateGraph(MarketAgentState)

    # Injection sécurisée de mc_runtime via _safe_node
    node_specs = [
        ("input_normalizer", input_normalizer),
        ("security_moderation", security_moderation),
        ("input_interpreter", input_interpreter),
        ("cognitive_guard", cognitive_guard),
        ("cognitive_orchestrator", cognitive_orchestrator),
        ("clarification_node", clarification_node),
        ("semantic_disambiguation", semantic_disambiguation),
        ("goal_planner", goal_planner),
        ("memory_update", memory_update),
        ("validator", validator),
        ("context_resolver", context_resolver),
        ("confirmation_gate", confirmation_gate),
        ("mcp_tool_executor", mcp_tool_executor),
        ("response_strategy", response_strategy),
        ("final_response", final_response),
        ("onboarding_node", onboarding_node),
    ]
    for name, fn in node_specs:
        workflow.add_node(name, partial(_safe_node(fn, name), mc_runtime=mc_runtime))

    workflow.set_entry_point("input_normalizer")

    # Liens du graphe de dialogue
    workflow.add_edge("input_normalizer", "security_moderation")

    workflow.add_conditional_edges(
        "security_moderation",
        _route_after_security,
        {"to_interpreter": "input_interpreter", "to_strategy": "response_strategy"},
    )

    workflow.add_edge("input_interpreter", "cognitive_guard")
    workflow.add_edge("cognitive_guard", "cognitive_orchestrator")
    
    workflow.add_conditional_edges(
        "cognitive_orchestrator",
        _route_after_cognitive,
        {"to_onboarding": "onboarding_node", "to_clarification": "clarification_node"},
    )

    workflow.add_conditional_edges(
        "clarification_node",
        _route_after_clarification,
        {"to_disambiguation": "semantic_disambiguation", "to_strategy": "response_strategy"},
    )

    workflow.add_conditional_edges(
        "semantic_disambiguation",
        _route_after_disambiguation,
        {"to_planner": "goal_planner", "to_strategy": "response_strategy"},
    )

    workflow.add_conditional_edges(
        "goal_planner",
        _route_after_planner,
        {"to_memory": "memory_update", "to_strategy": "response_strategy"},
    )

    workflow.add_edge("memory_update", "validator")

    workflow.add_conditional_edges(
        "validator",
        route_after_validator,
        {
            "to_resolver": "context_resolver",
            "to_confirmation": "confirmation_gate",
            "to_strategy": "response_strategy",
        },
    )

    workflow.add_conditional_edges(
        "context_resolver",
        _route_after_resolver,
        {"to_confirmation": "confirmation_gate", "to_strategy": "response_strategy"},
    )

    workflow.add_conditional_edges(
        "confirmation_gate",
        _route_after_confirmation,
        {"to_executor": "mcp_tool_executor", "to_strategy": "response_strategy"},
    )

    workflow.add_conditional_edges(
        "mcp_tool_executor",
        _route_after_executor,
        {"to_strategy": "response_strategy"},
    )

    workflow.add_edge("response_strategy", "final_response")
    workflow.add_edge("final_response", END)
    workflow.add_edge("onboarding_node", "response_strategy")

    logger.info("Market graph compiled successfully for role=%s", role_up)
    return workflow.compile(checkpointer=checkpointer)


__all__ = ["build_graph"]