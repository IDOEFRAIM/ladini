"""MarketBuyer adapter — point d'entrée DI-friendly pour l'orchestrateur.

Identique architecturalement à `market_coach.adapter`, mais compile le graphe
avec `role='BUYER'` via la factory commune `services.market.build_graph`.

Particularités du flux ACHETEUR :
  - intentions filtrées : CREATE_AUCTION, ACCEPT_BID, SELECT_WINNING_BID,
    GET_AUCTIONS_BIDS, etc. (cf. `interpreter_routing.BUYER_INTENTS`),
  - Context Resolver : `buyer_flow.buyer_context_resolver`,
  - dispatchers MCP : réutilisent `actions.MARKET_*_ACTIONS_MAP`
    (les outils MCP `create_auction`, `accept_bid`, `select_winning_bid`
    sont déjà câblés).
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict

from agriconnect.graphs.agents.common.output import AgriAgentOutput, ExpertMetadata
from agriconnect.graphs.agents.market_coach.state import MarketAgentState
from agriconnect.services.market.graph_builder import build_graph

logger = logging.getLogger("Agent.MarketBuyer.adapter")

_cache: Dict[str, Any] = {}


def get_agent_graph(llm_client: Any = None, mcp_session: Any = None, checkpointer: Any = None):
    """Retourne une application LangGraph BUYER compilée et mise en cache."""

    if mcp_session is None:
        logger.error("MarketBuyer initialized without mcp_session")
        raise ValueError("mcp_session is required for MarketBuyer")

    key = f"buyer_mcp_{id(mcp_session)}"
    if key not in _cache:
        _cache[key] = build_graph(
            role="BUYER",
            checkpointer=checkpointer,
            llm_client=llm_client,
            mcp_session=mcp_session,
        )
    return _cache[key]


def _build_initial_state(query: str, context: Dict[str, Any]) -> MarketAgentState:
    """Construit l'état initial conforme au contrat MarketAgentState (rôle BUYER)."""
    profile_ids = context.get("profile_ids") or {}
    user_id = context.get("user_id") or context.get("id") or "anonymous"
    user_phone = (
        context.get("user_phone")
        or context.get("phone")
        or context.get("whatsapp")
        or ""
    )
    session_id = (
        context.get("session_id")
        or context.get("thread_id")
        or str(uuid.uuid4())
    )

    state: MarketAgentState = {
        # 1. RAW INPUT
        "user_query": query,
        "normalized_text": "",
        "detected_language": context.get("detected_language") or "fr",
        "translated_text": "",
        "audio_file_path": context.get("audio_file_path"),
        "transcribed_audio": None,
        "timestamp": time.time(),
        # 2. USER / SESSION
        "user_phone": str(user_phone),
        "session_id": str(session_id),
        "user_role": "BUYER",
        "user_name": context.get("user_name"),
        "zone_name": context.get("zone_name"),
        "zone_id": context.get("zone_id") or profile_ids.get("zone"),
        "user_context_loaded": False,
        # 3. SECURITY
        "security_status": "SAFE",
        "trust_score": 1.0,
        "requires_human": False,
        # 4. INTERPRETER
        "interpreted_event": "UNKNOWN",
        "detected_intent": "UNKNOWN",
        "interpreter_confidence": 0.0,
        "extracted_entities": {},
        "raw_analysis": {},
        # 5. GOAL
        "current_goal": context.get("current_goal"),
        "goal_stack": list(context.get("goal_stack") or []),
        "goal_status": "ACTIVE",
        # 6. EXPECTATION
        "expected_input": context.get("expected_input") or "NONE",
        "last_agent_question": context.get("last_agent_question"),
        "expected_candidates": list(context.get("expected_candidates") or []),
        # 7. MEMORY
        "working_memory": dict(context.get("working_memory") or {}),
        "transaction_payload": dict(context.get("transaction_payload") or {}),
        "draft_payload": {},
        "stable_entities": {},
        "volatile_entities": {},
        "available_mapping": dict(context.get("available_mapping") or {}),
        # 8. SLOT TRACKING
        "required_fields": [],
        "missing_fields": [],
        "completed_fields": [],
        "validation_errors": [],
        "warnings": [],
        # 9. INTERRUPTIONS
        "interruption_detected": False,
        "interruption_payload": {},
        "suspended_payload": {},
        # 10. CONFIRMATION
        "waiting_for_confirmation": bool(context.get("waiting_for_confirmation")),
        "is_certified": False,
        "execution_authorized": False,
        "execution_result": {},
        # 11. MCP TOOL EXECUTION
        "selected_tool_args": {},
        "tool_execution_history": [],
        "retry_count": 0,
        # 13. SYSTEM FLAGS
        "status": "START",
        "is_locked": False,
        "should_replan": False,
        "should_interrupt": False,
    }
    return state


class MarketBuyer:
    """Wrapper expert MarketBuyer utilisé par l'orchestrateur."""

    def __init__(self, app: Any):
        self._app = app

    @classmethod
    def from_config(
        cls,
        llm_client: Any = None,
        mcp_session: Any = None,
        checkpointer: Any = None,
        **_: Any,
    ) -> "MarketBuyer":
        return cls(
            get_agent_graph(
                llm_client=llm_client,
                mcp_session=mcp_session,
                checkpointer=checkpointer,
            )
        )

    def _prepare_output(self, final_state: Dict[str, Any]) -> Dict[str, Any]:
        structured = (
            final_state.get("execution_result")
            or final_state.get("market_data")
            or {}
        )
        confidence = float(
            final_state.get("interpreter_confidence")
            or final_state.get("confidence")
            or 0.0
        )
        output = AgriAgentOutput(
            full_text=final_state.get("final_response", ""),
            structured_data=structured if isinstance(structured, dict) else {},
            expert_metadata=ExpertMetadata(
                name="market_buyer",
                confidence=confidence,
                sources=final_state.get("sources") or [],
            ),
        )
        return output.model_dump()

    async def run(self, query: str, context: Dict[str, Any]) -> Dict[str, Any]:
        user_id = context.get("user_id") or context.get("id") or "anonymous"
        config = {"configurable": {"thread_id": user_id}}
        initial_state = _build_initial_state(query, context)

        try:
            final = await self._app.ainvoke(initial_state, config=config)
            return self._prepare_output(final)
        except Exception as exc:
            logger.error("MarketBuyer execution failed: %s", exc, exc_info=True)
            return {
                "full_text": "Désolé, je rencontre une difficulté technique pour vous aider sur le marché.",
                "expert_metadata": {"name": "market_buyer", "confidence": 0.0},
            }

    def handle(self, query: str, context: Dict[str, Any]) -> Dict[str, Any]:
        user_id = context.get("user_id") or context.get("id") or "anonymous"
        config = {"configurable": {"thread_id": user_id}}
        initial_state = _build_initial_state(query, context)

        final = self._app.invoke(initial_state, config=config)
        return self._prepare_output(final)


__all__ = ["get_agent_graph", "MarketBuyer"]
