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

import asyncio
import logging
import uuid
from functools import partial
from typing import Any, Dict, List, Optional

from langgraph.graph import END, StateGraph
from langgraph.checkpoint.memory import MemorySaver

from agriconnect.graphs.agents.market_coach.core.state import MarketAgentState
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    build_runtime,
    build_runtime_from_session,
    _safe_node,
)
from agriconnect.graphs.agents.market_coach.nodes.role_guard import make_role_guard
from agriconnect.graphs.roles import normalize_role

from agriconnect.graphs.agents.market_coach.core.router import (
    DefaultDomainRouter,
    BUYER_CART_GOALS,
    BUYER_NEGOTIATION_GOALS,
)
from agriconnect.graphs.agents.market_coach.flows.buyer.flow import (
    cart_management,
    negotiation_gate,
)
from agriconnect.graphs.agents.market_coach.flows.buyer.order_tracking import (
    order_tracking_resolver,
)
from agriconnect.graphs.agents.market_coach.flows.producer.farm_logic import ensure_farm_node
from agriconnect.graphs.agents.market_coach.flows.common.onboarding import onboarding_node

from agriconnect.graphs.agents.market_coach.interpreter.routing import (
    goal_planner,
    make_input_interpreter,
)
from agriconnect.graphs.agents.market_coach.interpreter.strategy import response_strategy

from agriconnect.graphs.agents.market_coach.nodes.clarification import clarification_node
from agriconnect.graphs.agents.market_coach.nodes.cognitive import cognitive_guard, cognitive_orchestrator
from agriconnect.graphs.agents.market_coach.nodes.confirmation_gate import confirmation_gate
from agriconnect.graphs.agents.market_coach.nodes.executor import mcp_tool_executor
from agriconnect.graphs.agents.market_coach.nodes.form_node import form_node
from agriconnect.graphs.agents.market_coach.nodes.input_normalizer import input_normalizer
from agriconnect.graphs.agents.market_coach.nodes.memory import memory_update
from agriconnect.graphs.agents.market_coach.nodes.cleanup import post_response_cleanup
from agriconnect.graphs.agents.market_coach.nodes.cleaner import state_cleaner_node
from agriconnect.graphs.agents.market_coach.nodes.response_handlers import final_response
from agriconnect.graphs.agents.market_coach.nodes.routing import (
    _route_after_confirmation,
    _route_after_executor,
    _route_after_resolver,
    _route_after_security,
)
from agriconnect.graphs.agents.market_coach.nodes.security_moderation import security_moderation
from agriconnect.graphs.agents.market_coach.nodes.semantic_disambiguation import semantic_disambiguation
from agriconnect.graphs.agents.market_coach.nodes.ui_engine import ui_engine
from agriconnect.graphs.agents.market_coach.nodes.validation import validator

logger = logging.getLogger("AgriConnect.Market.GraphBuilder")

# Goals that are handled by the DRY form engine instead of direct slot-filling
_FORM_GOALS = {
    "SALES_PUBLISH_PRODUCT": "PRODUCT_CREATE",
    "PROCUREMENT_CREATE_REQUEST": "AUCTION_CREATE",
}

# Buyer transactional tunnel goals — centralisés dans core/router.py
# Importés ici uniquement pour la construction des edges LangGraph.


def _route_after_planner(state: MarketAgentState) -> str:
    """Route after goal_planner: form-eligible goals go to form_node."""
    goal = str(state.get("current_goal") or "").upper()

    if goal in BUYER_CART_GOALS:
        return "to_memory"

    # If a form is already active, route to form_node
    if state.get("active_form"):
        return "to_form"

    # If the planner just set a form-eligible goal, activate the form
    if goal in _FORM_GOALS:
        return "to_form"

    status = str(state.get("status") or "").upper()
    if status == "WAITING_INPUT":
        return "to_strategy"

    return "to_memory"


def _route_after_form(state: MarketAgentState) -> str:
    """Route after form_node.

    - If form is still collecting (WAITING_INPUT) → response_strategy
    - If form is asking for confirmation → response_strategy
    - If form completed → memory_update → validator pipeline
    """
    form_step = state.get("form_step")
    status = str(state.get("status") or "").upper()

    if form_step == "COMPLETE":
        return "to_memory"
    if status in {"WAITING_INPUT", "WAITING_CONFIRMATION"}:
        return "to_strategy"
    # Default: form is still collecting
    return "to_strategy"


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
    if (
        state.get("current_goal") == "DISAMBIGUATION_PENDING"
        and state.get("expected_input") == "SELECTION"
        and state.get("response_strategy") == "SELECTION_MENU"
    ):
        return "to_strategy"
    return "to_planner"


def _route_after_cognitive(state: MarketAgentState) -> str:
    """Route prioritaire vers le nœud d'onboarding lorsque nécessaire."""
    if state.get("is_onboarding"):
        return "to_onboarding"
    return "to_clarification"


# NOTE: _check_cart_status removed — caused infinite loops.
# cart_management ALWAYS flows to response_strategy; it handles
# errors/missing-fields internally by setting final_response.


def build_graph(
    role: str,
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
    role_up = normalize_role(role)
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
        # Orchestrator injecte WorkspaceCheckpointer pour la prod. Ce fallback
        # mémoire reste utile pour les tests/unitaires ou les demos isolées.
        checkpointer = MemorySaver()
    else:
        logger.info("Checkpointer injecté : %s", type(checkpointer).__name__)

    # Nœuds et routes spécialisés AG-UI
    input_interpreter = make_input_interpreter(role_up)
    domain_router = DefaultDomainRouter(role_up)

    # Le context_resolver est un wrapper autour du DomainRouter
    async def context_resolver(state, mc_runtime=None):
        result = await domain_router.resolve(state, mc_runtime)
        patch = dict(result.state_patch)
        if result.pending_menu is not None:
            patch["pending_menu"] = result.pending_menu
        return patch

    workflow = StateGraph(MarketAgentState)

    # Injection sécurisée de mc_runtime via _safe_node
    node_specs = [
        ("role_guard", make_role_guard(role_up)),
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
        ("ui_engine", ui_engine),
        ("ensure_farm_node", ensure_farm_node),
        ("confirmation_gate", confirmation_gate),
        ("mcp_tool_executor", mcp_tool_executor),
        ("response_strategy", response_strategy),
        ("state_cleaner", state_cleaner_node),
        ("final_response", final_response),
        ("post_response_cleanup", post_response_cleanup),
        ("onboarding_node", onboarding_node),
        ("form_node", form_node),
    ]
    # Nœuds dédiés au tunnel transactionnel Acheteur (sous-graphe buyer_flow).
    if role_up == "BUYER":
        node_specs.extend([
            ("cart_management", cart_management),
            ("negotiation_gate", negotiation_gate),
            ("order_tracking_node", order_tracking_resolver),
        ])

    for name, fn in node_specs:
        workflow.add_node(name, partial(_safe_node(fn, name), mc_runtime=mc_runtime))

    workflow.set_entry_point("role_guard")

    # Liens du graphe de dialogue
    workflow.add_edge("role_guard", "input_normalizer")
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
        {"to_memory": "memory_update", "to_form": "form_node", "to_strategy": "response_strategy"},
    )

    workflow.add_conditional_edges(
        "form_node",
        _route_after_form,
        {"to_memory": "memory_update", "to_strategy": "response_strategy"},
    )

    workflow.add_edge("memory_update", "validator")

    # Routage post-validateur : délégué au DomainRouter (rôle-agnostique).
    # Le DomainRouter injecte les branches transactionnelles buyer si nécessaire.
    route_after_validator = domain_router.route_after_validator

    if role_up == "BUYER":
        validator_targets = {
            "to_resolver": "context_resolver",
            "to_confirmation": "confirmation_gate",
            "to_strategy": "response_strategy",
            "to_cart": "cart_management",
            "to_negotiation": "negotiation_gate",
            "to_order_tracking": "order_tracking_node",
        }
    else:
        validator_targets = {
            "to_resolver": "context_resolver",
            "to_confirmation": "confirmation_gate",
            "to_strategy": "response_strategy",
        }

    workflow.add_conditional_edges("validator", route_after_validator, validator_targets)

    if role_up == "BUYER":
        # Tous les nœuds buyer doivent passer par ui_engine pour convertir
        # pending_menu → ag_ui_component (sinon pas de mapping/candidats).
        workflow.add_edge("cart_management", "ui_engine")
        workflow.add_edge("negotiation_gate", "ui_engine")
        workflow.add_edge("order_tracking_node", "ui_engine")

    workflow.add_conditional_edges(
        "context_resolver",
        _route_after_resolver,
        {"to_confirmation": "confirmation_gate", "to_strategy": "ui_engine", "to_farm_guard": "ensure_farm_node", "to_form": "form_node"},
    )

    # ui_engine transforme pending_menu → ag_ui_component puis passe à response_strategy
    workflow.add_edge("ui_engine", "response_strategy")

    workflow.add_edge("ensure_farm_node", "confirmation_gate")

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

    workflow.add_edge("response_strategy", "state_cleaner")
    workflow.add_edge("state_cleaner", "final_response")
    workflow.add_edge("final_response", "post_response_cleanup")
    workflow.add_edge("post_response_cleanup", END)
    workflow.add_edge("onboarding_node", "response_strategy")

    logger.info("Market graph compiled successfully for role=%s", role_up)
    return workflow.compile(checkpointer=checkpointer)


__all__ = ["build_graph"]

import asyncio


async def test_onboarding_flow() -> None:
    """Legacy helper: exige un accès LLM réel pour l'extraction complète."""
    async with build_runtime() as mc_runtime:
        app = build_graph(role="BUYER", mc_runtime=mc_runtime)
        config = {"configurable": {"thread_id": "test_session_flow_01"}}

        conversation_steps = [
            ("Bonjour je suis un acheteur", "COLLECT_NAME"),
            ("Je m'appelle Jean Dupont", "COLLECT_ZONE"),
            ("Je suis à Bobo Dioulasso", "COMPLETED"),
            ("okay je suis d'accord", "COMPLETED"),
        ]

        current_state = {"user_phone": "+22631479808", "transaction_payload": {}}

        for user_input, expected_step in conversation_steps:
            print(f"\n--- Simulation input: '{user_input}' ---")
            current_state["user_query"] = user_input
            final_state = await app.ainvoke(current_state, config=config)
            current_state.update(final_state)

            step = final_state.get("onboarding_step")
            name = final_state.get("transaction_payload", {}).get("name")
            zone = final_state.get("transaction_payload", {}).get("zone_name")

            print(f"Étape actuelle : {step}")
            print(f"Payload actuel : Name={name}, Zone={zone}")
            print(f"Réponse agent : {final_state.get('final_response')}")

            if step == expected_step or (
                expected_step == "COMPLETED" and str(final_state.get("status")).upper() == "COMPLETED"
            ):
                print(f"✅ Étape {step} validée.")
            else:
                print(f"⚠️ Échec à l'étape {step} (attendu {expected_step})")


async def demo_buyer_purchase_flow() -> None:
    """Démonstration déterministe du parcours Acheteur (panier → précommande → négociation).

    Cette démo n'utilise pas le graphe complet : elle cible directement les nodes métier
    pour valider la logique côté buyer en s'appuyant sur un runtime MCP fictif.
    """

    class DemoRuntime:
        async def call_db(self, tool_name: str, **kwargs: Any) -> Dict[str, Any]:  # noqa: D401
            if tool_name == "search_products":
                return {
                    "status": "success",
                    "results": [
                        {
                            "id": "maize-001",
                            "name": "Maïs blanc",
                            "price": 250,
                            "unit": "KG",
                            "vendor": {"name": "Ferme Koudougou"},
                        }
                    ],
                }
            if tool_name == "validate_stock_availability_atomic":
                return {
                    "status": "SUCCESS",
                    "unit_price": 260,
                    "unit": "KG",
                    "producer_id": "farm-001",
                    "message": "Stock réservé",
                }
            if tool_name == "initiate_negotiation_session":
                return {
                    "status": "PENDING",
                    "negotiation_id": "neg-001",
                    "auction_id": "neg-001",
                    "product_id": "maize-001",
                    "producer_id": "farm-001",
                    "buyer_offer": kwargs.get("offered_price", 240),
                    "seller_minimum": 260,
                    "price_gap": 20,
                    "message": "🤝 Négociation ouverte sur Maïs blanc.",
                }
            if tool_name == "get_auction_bids":
                return {
                    "status": "success",
                    "bids": [
                        {
                            "bid_id": "bid-001",
                            "producer_name": "Ferme A",
                            "price": 255,
                            "product": "Maïs blanc",
                        },
                        {
                            "bid_id": "bid-002",
                            "producer_name": "Ferme B",
                            "price": 258,
                            "product": "Maïs blanc",
                        },
                    ],
                }
            if tool_name == "select_winning_bid":
                return {
                    "status": "success",
                    "summary_buyer": "✅ Offre acceptée pour Maïs blanc.",
                }
            return {"status": "success", "message": f"{tool_name} stubbed"}

    runtime = DemoRuntime()

    from agriconnect.graphs.agents.market_coach.flows.buyer.flow import (
        _create_preorder,
        build_product_selection_menu,
    )

    def _log(title: str, result: Dict[str, Any]) -> None:
        print(f"\n[{title}] status={result.get('status')} strategy={result.get('response_strategy')}")
        if result.get("final_response"):
            print(result["final_response"])
        if result.get("pending_menu"):
            menu = result["pending_menu"]
            print(f"Menu → {menu.title} ({len(menu.options)} options)")

    state: Dict[str, Any] = {
        "user_phone": "+22670000000",
        "current_goal": "BUYER_ADD_TO_CART",
        "transaction_payload": {"product": "maïs blanc", "quantity_mentioned": 50, "unit_mentioned": "KG"},
        "preorder_workflow": {"phase": "CART"},
    }

    # Step 1: Add to cart (triggers vendor resolution)
    cart_result = await cart_management(state, runtime)
    state.update(cart_result)
    _log("1. Ajout panier (résolution produit)", cart_result)

    # Step 1b: Simulate multi-vendor selection (demo)
    if state.get("vendor_selection_context") and not state["vendor_selection_context"].get("__reset__"):
        print("\n[1b] Multi-vendor menu detected — simulating selection of vendor #1")
        state["transaction_payload"] = {"selection_index": 1, "product": "maïs blanc", "quantity_mentioned": 50}
        state["current_goal"] = "BUYER_ADD_TO_CART"
        cart_result2 = await cart_management(state, runtime)
        state.update(cart_result2)
        _log("1b. Vendor selection → cart insertion", cart_result2)

    # Step 2: Preorder draft (pre-flight recap)
    state["current_goal"] = "BUYER_PREORDER_INIT"
    state["transaction_payload"] = {}
    draft_result = await _create_preorder(state, runtime)
    state.update(draft_result)
    _log("2. Précommande brouillon (pre-flight recap)", draft_result)

    # Step 3: Confirm preorder
    state["current_goal"] = "BUYER_PREORDER_CONFIRM"
    state["transaction_payload"] = {"resolved_id": "PREORDER_CONFIRM"}
    confirm_result = await _create_preorder(state, runtime)
    state.update(confirm_result)
    _log("3. Confirmation précommande", confirm_result)

    # Step 4: Open negotiation
    negotiation_state: Dict[str, Any] = {
        "user_phone": state["user_phone"],
        "current_goal": "BUYER_NEGOTIATE_PRICE",
        "transaction_payload": {
            "product": "maïs blanc",
            "quantity_mentioned": 50,
            "price_mentioned": 240,
        },
        "stable_entities": {},
    }

    neg_open = await negotiation_gate(negotiation_state, runtime)
    negotiation_state.update(neg_open)
    _log("4. Ouverture négociation", neg_open)

    # Step 5: View offers
    negotiation_state["transaction_payload"] = {"resolved_id": "NEGOTIATION_VIEW_OFFERS"}
    neg_offers = await negotiation_gate(negotiation_state, runtime)
    negotiation_state.update(neg_offers)
    _log("5. Consultation offres", neg_offers)

    # Step 6: Counter-offer
    negotiation_state["transaction_payload"] = {"resolved_id": "NEGOTIATION_COUNTER"}
    neg_counter = await negotiation_gate(negotiation_state, runtime)
    negotiation_state.update(neg_counter)
    _log("6. Contre-offre (demande prix)", neg_counter)

    # Step 7: Submit counter price
    negotiation_state["transaction_payload"] = {"price_mentioned": 260}
    neg_submit = await negotiation_gate(negotiation_state, runtime)
    negotiation_state.update(neg_submit)
    _log("7. Soumission contre-offre", neg_submit)

    # Step 8: Accept a bid
    negotiation_state["transaction_payload"] = {"resolved_id": "NEGOTIATION_VIEW_OFFERS"}
    neg_offers2 = await negotiation_gate(negotiation_state, runtime)
    negotiation_state.update(neg_offers2)
    negotiation_state["transaction_payload"] = {"bid_id": "bid-001"}
    neg_accept = await negotiation_gate(negotiation_state, runtime)
    _log("8. Acceptation offre finale", neg_accept)

    print("\n✅ Parcours buyer complet simulé avec succès (données stubs).")


if __name__ == "__main__":
    asyncio.run(demo_buyer_purchase_flow())