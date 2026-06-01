"""MarketBuyer — façade légère + script de simulation BUYER.

Mirror du shim `market_coach.nodes`. Toute la logique est partagée via
`agriconnect.services.market.*`. Cet agent active la spécialisation BUYER
au niveau de l'interpréteur et du Context Resolver.

Pour un test rapide en CLI :
    python -m agriconnect.graphs.agents.market_buyer.nodes
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
    goal_planner,
    make_input_interpreter,
)
from agriconnect.services.market.shared_core import (
    confirmation_gate,
    final_response,
    input_normalizer,
    mcp_tool_executor,
    memory_update,
    response_strategy,
    security_moderation,
    validator,
)

logger = logging.getLogger("AgriConnect.MarketBuyer.Nodes")

# Spécialisation BUYER de l'interpréteur (filtre les intents producteur)
input_interpreter = make_input_interpreter("BUYER")
context_resolver = buyer_context_resolver


def build(
    mc_runtime: Optional[MarketRuntime] = None,
    checkpointer: Any = None,
    llm_client: Any = None,
    mcp_session: Any = None,
):
    """Compile le StateGraph LangGraph du MarketBuyer (rôle BUYER)."""
    return build_graph(
        role="BUYER",
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
]


# =====================================================================
# 🧪 SCRIPT DE SIMULATION (WhatsApp CREATE_AUCTION acheteur)
# =====================================================================

async def simuler_dialogue_whatsapp_buyer_auction():
    print("🚀 Initialisation du StateGraph BUYER (Flux CREATE_AUCTION)...")

    from langgraph.checkpoint.memory import MemorySaver
    checkpointer = MemorySaver()

    async with build_runtime() as mc_runtime:
        app = build(checkpointer=checkpointer, mc_runtime=mc_runtime)
        config = {"configurable": {"thread_id": "acheteur_auction_test_2026"}}

        state = {
            "user_phone": "+22678143821",
            "session_id": "whatsapp_session_buyer_001",
            "user_role": "BUYER",
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

        # Scénario : un acheteur lance un appel d'offres pour du mil
        messages_test = [
            "Je veux acheter 5 tonnes de mil à 250 FCFA le kilo",
            "Bobo-Dioulasso",  # zone
            "OK",  # confirmation
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
                print(f"🔹 Champs Manquants       : {state.get('missing_fields')}")
                print(f"\n💬 RÉPONSE ROUTÉE VERS LE SMARTPHONE DE L'ACHETEUR : \n")
                print("--------------------------------------------------")
                print(state.get('final_response'))
                print("--------------------------------------------------")
            except Exception as e:
                logger.error(f"💥 Le graphe a crashé au tour {i} : {str(e)}", exc_info=True)
                break


if __name__ == "__main__":
    asyncio.run(simuler_dialogue_whatsapp_buyer_auction())
