"""MarketCoach — Façade légère + Script de simulation.

Ce fichier est désormais un *shim* mince. La logique métier vit dans :

  - `agriconnect.services.market.shared_core`        (8 nœuds universels)
  - `agriconnect.services.market.interpreter_routing` (input_interpreter + goal_planner)
  - `agriconnect.services.market.producer_flow`       (Context Resolver Producteur)
  - `agriconnect.services.market.buyer_flow`          (Context Resolver Acheteur)
  - `agriconnect.services.market.graph_builder`       (`build_graph(role)`)

Les imports historiques (`from agriconnect.graphs.agents.market_coach.nodes import …`)
restent fonctionnels grâce aux re-exports ci-dessous.

`build()` conserve sa signature originale et compile un graphe `role="PRODUCER"`,
ce qui préserve la stabilité de l'agent Producteur déjà validé en production.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    build_runtime,
    build_runtime_from_session,
)
from agriconnect.services.market.buyer_flow import buyer_context_resolver
from agriconnect.services.market.graph_builder import build_graph
from agriconnect.services.market.interpreter_routing import (
    _interpret_fast_path,
    _remap_entities,
    goal_planner,
    make_input_interpreter,
)
from agriconnect.services.market.producer_flow import (
    _resolve_auction,
    _resolve_bid,
    _resolve_my_bids,
    _resolve_stock,
    producer_context_resolver,
)
from agriconnect.services.market.shared_core import (
    _READ_GOALS,
    _WRITE_GOALS,
    _maybe_await,
    _normalize_text,
    _now,
    _safe_node,
    confirmation_gate,
    final_response,
    input_normalizer,
    mcp_tool_executor,
    memory_update,
    reset_error_status,
    response_strategy,
    security_moderation,
    validator,
)

logger = logging.getLogger("AgriConnect.MarketCoach.Nodes")

# Backward-compat : le nœud `input_interpreter` historique = variante PRODUCER
input_interpreter = make_input_interpreter("PRODUCER")
# Backward-compat : `context_resolver` = variante PRODUCER
context_resolver = producer_context_resolver


def build(
    mc_runtime: Optional[MarketRuntime] = None,
    checkpointer: Any = None,
    llm_client: Any = None,
    mcp_session: Any = None,
):
    """Compile le StateGraph LangGraph du MarketCoach (rôle PRODUCER).

    Préservé tel quel pour la compat ascendante. Pour un agent ACHETEUR,
    utiliser directement `agriconnect.services.market.build_graph(role='BUYER')`.
    """
    return build_graph(
        role="PRODUCER",
        mc_runtime=mc_runtime,
        checkpointer=checkpointer,
        llm_client=llm_client,
        mcp_session=mcp_session,
    )


__all__ = [
    "build",
    "input_normalizer",
    "security_moderation",
    "input_interpreter",
    "goal_planner",
    "memory_update",
    "validator",
    "context_resolver",
    "confirmation_gate",
    "mcp_tool_executor",
    "response_strategy",
    "final_response",
    # Helpers / constants (re-exports)
    "MarketRuntime",
    "build_runtime",
    "build_runtime_from_session",
    "_READ_GOALS",
    "_WRITE_GOALS",
    "_safe_node",
    "_normalize_text",
    "_maybe_await",
    "_now",
    "reset_error_status",
    "_interpret_fast_path",
    "_remap_entities",
    # Producer helpers
    "_resolve_auction",
    "_resolve_my_bids",
    "_resolve_bid",
    "_resolve_stock",
    "producer_context_resolver",
    "buyer_context_resolver",
]


# =====================================================================
# 🧪 SCRIPT DE SIMULATION (WhatsApp PLACE_BID proactif)
# =====================================================================
# Conservé pour la régression du flux Producteur "tomate".

async def simuler_dialogue_whatsapp_bid_proactif():
    print("🚀 Initialisation du VRAI StateGraph compilé (Flux Proactif PLACE_BID)...")

    from langgraph.checkpoint.memory import MemorySaver
    checkpointer = MemorySaver()

    async with build_runtime() as mc_runtime:
        app = build(checkpointer=checkpointer, mc_runtime=mc_runtime)
        config = {"configurable": {"thread_id": "producteur_bid_test_2026_ultra"}}

        state = {
            "user_phone": "+22601479800",
            "session_id": "whatsapp_session_bid_999",
            "transaction_payload": {},
            "extracted_entities": {},
            "available_mapping": {},
            "missing_fields": [],
            "validation_errors": [],
            "execution_authorized": False,
            "retry_count": 0,
            "status": "START",
            "current_goal": None,
            "response_strategy": None,
            "final_response": None,
        }

        messages_test = [
            "Je veux faire une offre pour le mais s'il vous plaît",
            "1",
            "300120 fcfa",
            "OK",
        ]

        for i, message in enumerate(messages_test, 1):
            print(f"\n{'-'*60}\n📥 WHATSAPP ENTRANT {i} : '{message}'\n{'-'*60}")
            state["user_query"] = message
            if i > 1:
                state["final_response"] = None
                state["response_strategy"] = None
                state["extracted_entities"] = {}

            try:
                state = await app.ainvoke(state, config=config)
                print(f"\n📊 --- STATUT DU STATE APRÈS LE MESSAGE {i} ---")
                print(f"🔹 Événement Interprété    : {state.get('interpreted_event')}")
                print(f"🔹 Objectif Actuel        : {state.get('current_goal')} (Status: {state.get('status')})")
                print(f"📦 Payload Temporaire      : {state.get('transaction_payload')}")
                print(f"🧠 Mapping WhatsApp Actif : {state.get('available_mapping')}")
                print(f"🔹 Champs Manquants       : {state.get('missing_fields')}")
                print(f"🔹 Statut Machine Intern  : {state.get('status')}")
                print(f"\n💬 RÉPONSE ROUTÉE VERS LE SMARTPHONE DU PRODUCTEUR : \n")
                print("--------------------------------------------------")
                print(state.get('final_response'))
                print("--------------------------------------------------")
            except Exception as e:
                logger.error(f"💥 Le graphe a crashé au tour {i} : {str(e)}", exc_info=True)
                break


if __name__ == "__main__":
    asyncio.run(simuler_dialogue_whatsapp_bid_proactif())
