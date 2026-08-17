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
from functools import partial
from typing import Any, Dict, List, Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from agriconnect.graphs.agents.market_coach.core.goals import BUYER_CART_GOALS
from agriconnect.graphs.agents.market_coach.core.policies import get_fast_path_policy
from agriconnect.graphs.agents.market_coach.core.router import get_domain_router
from agriconnect.graphs.agents.market_coach.core.state import MarketAgentState
from agriconnect.graphs.agents.market_coach.flows.buyer.flow import (
    cart_management,
    negotiation_gate,
)
from agriconnect.graphs.agents.market_coach.flows.buyer.order_tracking import (
    order_tracking_resolver,
)
from agriconnect.graphs.agents.market_coach.flows.common.onboarding import (
    onboarding_node,
)
from agriconnect.graphs.agents.market_coach.flows.producer.farm_logic import (
    ensure_farm_node,
)
from agriconnect.graphs.agents.market_coach.interpreter.routing import (
    goal_planner,
    make_input_interpreter,
)
from agriconnect.graphs.agents.market_coach.interpreter.strategy import (
    response_strategy,
)
from agriconnect.graphs.agents.market_coach.nodes.clarification import (
    clarification_node,
)
from agriconnect.graphs.agents.market_coach.nodes.cleaner import state_cleaner_node
from agriconnect.graphs.agents.market_coach.nodes.cleanup import post_response_cleanup
from agriconnect.graphs.agents.market_coach.nodes.cognitive import (
    cognitive_guard,
    cognitive_orchestrator,
)
from agriconnect.graphs.agents.market_coach.nodes.confirmation_gate import (
    confirmation_gate,
)
from agriconnect.graphs.agents.market_coach.nodes.executor import mcp_tool_executor
from agriconnect.graphs.agents.market_coach.nodes.input_normalizer import (
    input_normalizer,
)
from agriconnect.graphs.agents.market_coach.nodes.memory import memory_update
from agriconnect.graphs.agents.market_coach.nodes.response_handlers import (
    final_response,
)
from agriconnect.graphs.agents.market_coach.nodes.role_guard import make_role_guard
from agriconnect.graphs.agents.market_coach.nodes.routing import (
    _route_after_confirmation,
    _route_after_executor,
    _route_after_resolver,
    _route_after_security,
)
from agriconnect.graphs.agents.market_coach.nodes.security_moderation import (
    security_moderation,
)
from agriconnect.graphs.agents.market_coach.nodes.semantic_disambiguation import (
    semantic_disambiguation,
)
from agriconnect.graphs.agents.market_coach.nodes.ui_engine import ui_engine
from agriconnect.graphs.agents.market_coach.nodes.validation import validator
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    _safe_node,
    build_runtime,
    build_runtime_from_session,
)
from agriconnect.graphs.roles import normalize_role

logger = logging.getLogger("AgriConnect.Market.GraphBuilder")

# Moteur de collecte de slots UNIFIÉ : tous les intents WRITE passent par le
# pipeline générique (validator + memory_update + rendering/ask). Le moteur
# formulaire DRY (`form_node` + `agents/forms.py`) a été retiré côté MarketCoach
# (dette architecturale éliminée) — `agents/forms.py` reste utilisé par
# `src/futur/formation`, on ne le touche pas.

# Buyer transactional tunnel goals — centralisés dans core/goals.py
# (dérivés d'INTENT_CONFIG). Importés ici uniquement pour les edges.


def _route_after_planner(state: MarketAgentState) -> str:
    """Route after goal_planner. Moteur de collecte unifié : TOUT passe par le
    pipeline générique (memory_update → validator → ...). Plus aucun goal ne
    route vers form_node (retiré — voir dette moteur formulaire éliminée)."""
    goal = str(state.get("current_goal") or "").upper()

    if goal in BUYER_CART_GOALS:
        return "to_memory"

    status = str(state.get("status") or "").upper()
    if status == "WAITING_INPUT":
        return "to_strategy"

    return "to_memory"


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


def _make_route_after_interpreter(role: str):
    """Conditional router after input_interpreter.

    Delegates to FastPathPolicy — new tunnel goals are added to the policy
    registry in core/policies.py, not here. Refonte double-rôle : le graphe
    est désormais unique (tunnels acheteur toujours présents), donc la
    politique la plus permissive (`for_buyer`, superset strict de
    `for_producer`) s'applique quel que soit le paramètre `role` reçu.
    """
    policy = get_fast_path_policy("BUYER")
    return policy.route


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
            mc_runtime = build_runtime_from_session(
                llm_client=llm_client, mcp_session=mcp_session
            )
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
    domain_router = get_domain_router(role_up)

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
    ]
    # Nœuds du tunnel transactionnel Acheteur — désormais TOUJOURS présents :
    # refonte double-rôle, tout utilisateur peut acheter ET vendre au sein de
    # la même conversation (plus de topologie de graphe conditionnée au rôle).
    node_specs.extend(
        [
            ("cart_management", cart_management),
            ("negotiation_gate", negotiation_gate),
            ("order_tracking_node", order_tracking_resolver),
        ]
    )

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

    route_after_interpreter = _make_route_after_interpreter(role_up)
    workflow.add_conditional_edges(
        "input_interpreter",
        route_after_interpreter,
        {"to_cognitive": "cognitive_guard", "to_memory_fast": "memory_update"},
    )

    workflow.add_edge("cognitive_guard", "cognitive_orchestrator")

    workflow.add_conditional_edges(
        "cognitive_orchestrator",
        _route_after_cognitive,
        {"to_onboarding": "onboarding_node", "to_clarification": "clarification_node"},
    )

    workflow.add_conditional_edges(
        "clarification_node",
        _route_after_clarification,
        {
            "to_disambiguation": "semantic_disambiguation",
            "to_strategy": "response_strategy",
        },
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

    # Routage post-validateur : délégué au DomainRouter (règles par rôle).
    # Nouveaux tunnels s'ajoutent dans core/goals.py + core/router.py, pas ici.
    route_after_validator = domain_router.decide

    # Cibles toujours complètes (union producteur+acheteur) — un même
    # utilisateur peut déclencher n'importe quel tunnel selon le goal classé.
    validator_targets = {
        "to_resolver": "context_resolver",
        "to_confirmation": "confirmation_gate",
        "to_strategy": "response_strategy",
        "to_cart": "cart_management",
        "to_negotiation": "negotiation_gate",
        "to_order_tracking": "order_tracking_node",
    }

    workflow.add_conditional_edges(
        "validator", route_after_validator, validator_targets
    )

    # Tous les nœuds buyer doivent passer par ui_engine pour convertir
    # pending_menu → ag_ui_component (sinon pas de mapping/candidats).
    workflow.add_edge("cart_management", "ui_engine")
    workflow.add_edge("negotiation_gate", "ui_engine")
    workflow.add_edge("order_tracking_node", "ui_engine")

    workflow.add_conditional_edges(
        "context_resolver",
        _route_after_resolver,
        {
            "to_confirmation": "confirmation_gate",
            "to_strategy": "ui_engine",
            "to_farm_guard": "ensure_farm_node",
        },
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


COMMAND_TEST_PHONE = "+22601479800"
DEFAULT_REAL_DIALOG: List[str] = [
    "Bonjour, je veux acheter du maïs",
    "Je veux ajouter 50 kg de maïs blanc",
    "précommander",
    "confirmer la commande",
    "ouvrir négociation",
    "montre les offres",
    "je veux faire une contre offre",
    "je propose 260",
    "accepter offre 1",
]


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
        # ── Producteur : création de produit / déclaration de production future ──
        if tool_name == "get_producer_farm":
            # Une seule ferme -> auto-résolution silencieuse par ensure_farm_node
            # et producer_context_resolver._resolve_default_farm (0 appel réseau
            # supplémentaire, farm_id injecté directement dans transaction_payload).
            return {
                "status": "success",
                "data": [
                    {
                        "id": "farm-demo-01",
                        "name": "Ferme Démo",
                        "location": "Bobo-Dioulasso",
                    }
                ],
            }
        if tool_name == "get_or_create_farm":
            return {"status": "success", "id": "farm-demo-01", "name": "Ferme Démo"}
        if tool_name == "create_product":
            return {
                "status": "success",
                "message": f"✅ {kwargs.get('name') or kwargs.get('product_name')} publié sur le marché.",
                "data": {
                    "id": "prod-demo-01",
                    "name": kwargs.get("name") or kwargs.get("product_name"),
                },
            }
        if tool_name == "declare_future_production":
            return {
                "status": "success",
                "message": "✅ Production future déclarée.",
                "data": {
                    "id": "cycle-demo-01",
                    "product": kwargs.get("product"),
                    "estimated_available_at": kwargs.get("estimated_available_at"),
                },
            }
        return {"status": "success", "message": f"{tool_name} stubbed"}


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

        current_state = {"user_phone": COMMAND_TEST_PHONE, "transaction_payload": {}}

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
                expected_step == "COMPLETED"
                and str(final_state.get("status")).upper() == "COMPLETED"
            ):
                print(f"✅ Étape {step} validée.")
            else:
                print(f"⚠️ Échec à l'étape {step} (attendu {expected_step})")


async def demo_buyer_purchase_flow() -> None:
    """Démonstration déterministe du parcours Acheteur (panier → précommande → négociation)."""

    runtime = DemoRuntime()

    from agriconnect.graphs.agents.market_coach.flows.buyer.flow import (
        _create_preorder,
    )

    def _log(title: str, result: Dict[str, Any]) -> None:
        print(
            f"\n[{title}] status={result.get('status')} strategy={result.get('response_strategy')}"
        )
        if result.get("final_response"):
            print(result["final_response"])
        if result.get("pending_menu"):
            menu = result["pending_menu"]
            print(f"Menu → {menu.title} ({len(menu.options)} options)")

    state: Dict[str, Any] = {
        "user_phone": COMMAND_TEST_PHONE,
        "current_goal": "BUYER_ADD_TO_CART",
        "transaction_payload": {"product": "maïs blanc", "quantity": 50, "unit": "KG"},
        "preorder_workflow": {"phase": "CART"},
    }

    # Step 1: Add to cart (triggers vendor resolution)
    cart_result = await cart_management(state, runtime)
    state.update(cart_result)
    _log("1. Ajout panier (résolution produit)", cart_result)

    # Step 1b: Simulate multi-vendor selection (demo)
    if state.get("vendor_selection_context") and not state[
        "vendor_selection_context"
    ].get("__reset__"):
        print("\n[1b] Multi-vendor menu detected — simulating selection of vendor #1")
        state["transaction_payload"] = {
            "selection_index": 1,
            "product": "maïs blanc",
            "quantity": 50,
        }
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
            "quantity": 50,
            "price": 240,
        },
        "stable_entities": {},
    }

    neg_open = await negotiation_gate(negotiation_state, runtime)
    negotiation_state.update(neg_open)
    _log("4. Ouverture négociation", neg_open)

    # Step 5: View offers
    negotiation_state["transaction_payload"] = {
        "resolved_id": "NEGOTIATION_VIEW_OFFERS"
    }
    neg_offers = await negotiation_gate(negotiation_state, runtime)
    negotiation_state.update(neg_offers)
    _log("5. Consultation offres", neg_offers)

    # Step 6: Counter-offer
    negotiation_state["transaction_payload"] = {"resolved_id": "NEGOTIATION_COUNTER"}
    neg_counter = await negotiation_gate(negotiation_state, runtime)
    negotiation_state.update(neg_counter)
    _log("6. Contre-offre (demande prix)", neg_counter)

    # Step 7: Submit counter price
    negotiation_state["transaction_payload"] = {"price": 260}
    neg_submit = await negotiation_gate(negotiation_state, runtime)
    negotiation_state.update(neg_submit)
    _log("7. Soumission contre-offre", neg_submit)

    # Step 8: Accept a bid
    negotiation_state["transaction_payload"] = {
        "resolved_id": "NEGOTIATION_VIEW_OFFERS"
    }
    neg_offers2 = await negotiation_gate(negotiation_state, runtime)
    negotiation_state.update(neg_offers2)
    negotiation_state["transaction_payload"] = {"bid_id": "bid-001"}
    neg_accept = await negotiation_gate(negotiation_state, runtime)
    _log("8. Acceptation offre finale", neg_accept)

    print("\n✅ Parcours buyer complet simulé avec succès (données stubs).")


def _is_success_status(result: Dict[str, Any]) -> bool:
    status = str(result.get("status") or "").upper()
    return status not in {"ERROR", "FAILED"}


async def run_command_logic_test_suite() -> None:
    """Set de tests réalistes pour valider la logique de commande acheteur."""

    from agriconnect.graphs.agents.market_coach.flows.buyer.flow import _create_preorder

    runtime = DemoRuntime()
    phone = COMMAND_TEST_PHONE
    test_results: List[Dict[str, Any]] = []

    state: Dict[str, Any] = {
        "user_phone": phone,
        "current_goal": "BUYER_ADD_TO_CART",
        "transaction_payload": {"product": "maïs blanc", "quantity": 50, "unit": "KG"},
        "preorder_workflow": {"phase": "CART"},
    }

    cart_result = await cart_management(state, runtime)
    state.update(cart_result)
    test_results.append(
        {
            "label": "Cart → sélection produit",
            "result": cart_result,
            "success": _is_success_status(cart_result),
        }
    )

    if state.get("vendor_selection_context") and not state[
        "vendor_selection_context"
    ].get("__reset__"):
        state["transaction_payload"] = {
            "selection_index": 1,
            "product": "maïs blanc",
            "quantity": 50,
        }
        state["current_goal"] = "BUYER_ADD_TO_CART"
        cart_result2 = await cart_management(state, runtime)
        state.update(cart_result2)
        test_results.append(
            {
                "label": "Cart → choix vendeur",
                "result": cart_result2,
                "success": _is_success_status(cart_result2),
            }
        )

    state["current_goal"] = "BUYER_PREORDER_INIT"
    state["transaction_payload"] = {}
    draft_result = await _create_preorder(state, runtime)
    state.update(draft_result)
    test_results.append(
        {
            "label": "Précommande brouillon",
            "result": draft_result,
            "success": _is_success_status(draft_result),
        }
    )

    state["current_goal"] = "BUYER_PREORDER_CONFIRM"
    state["transaction_payload"] = {"resolved_id": "PREORDER_CONFIRM"}
    confirm_result = await _create_preorder(state, runtime)
    state.update(confirm_result)
    test_results.append(
        {
            "label": "Précommande confirmation",
            "result": confirm_result,
            "success": _is_success_status(confirm_result),
        }
    )

    negotiation_state: Dict[str, Any] = {
        "user_phone": phone,
        "current_goal": "BUYER_NEGOTIATE_PRICE",
        "transaction_payload": {
            "product": "maïs blanc",
            "quantity": 50,
            "price": 240,
        },
        "stable_entities": {},
    }

    neg_open = await negotiation_gate(negotiation_state, runtime)
    negotiation_state.update(neg_open)
    test_results.append(
        {
            "label": "Négociation ouverture",
            "result": neg_open,
            "success": _is_success_status(neg_open),
        }
    )

    negotiation_state["transaction_payload"] = {
        "resolved_id": "NEGOTIATION_VIEW_OFFERS"
    }
    neg_offers = await negotiation_gate(negotiation_state, runtime)
    negotiation_state.update(neg_offers)
    test_results.append(
        {
            "label": "Négociation – offres",
            "result": neg_offers,
            "success": _is_success_status(neg_offers),
        }
    )

    negotiation_state["transaction_payload"] = {"resolved_id": "NEGOTIATION_COUNTER"}
    neg_counter = await negotiation_gate(negotiation_state, runtime)
    negotiation_state.update(neg_counter)
    test_results.append(
        {
            "label": "Négociation – demande contre-offre",
            "result": neg_counter,
            "success": _is_success_status(neg_counter),
        }
    )

    negotiation_state["transaction_payload"] = {"price": 260}
    neg_submit = await negotiation_gate(negotiation_state, runtime)
    negotiation_state.update(neg_submit)
    test_results.append(
        {
            "label": "Négociation – soumission prix",
            "result": neg_submit,
            "success": _is_success_status(neg_submit),
        }
    )

    negotiation_state["transaction_payload"] = {
        "resolved_id": "NEGOTIATION_VIEW_OFFERS"
    }
    neg_offers2 = await negotiation_gate(negotiation_state, runtime)
    negotiation_state.update(neg_offers2)
    negotiation_state["transaction_payload"] = {"bid_id": "bid-001"}
    neg_accept = await negotiation_gate(negotiation_state, runtime)
    test_results.append(
        {
            "label": "Négociation – acceptation offre",
            "result": neg_accept,
            "success": _is_success_status(neg_accept),
        }
    )

    print("\n=== RAPPORT TEST LOGIQUE COMMANDE ===")
    global_success = True
    for entry in test_results:
        success = entry["success"]
        result = entry["result"]
        status = result.get("status")
        print(
            f"[{entry['label']}] {'✅' if success else '❌'} status={status} strategy={result.get('response_strategy')}"
        )
        if not success:
            global_success = False
            print(f"   ↪ Détails: {result}")

    if global_success:
        print(
            "✅ Tous les scénarios critiques de commande ont abouti sans erreur (runtime démo)."
        )
    else:
        print("❌ Des erreurs ont été détectées — inspecter les logs ci-dessus.")


async def run_manual_smoke_tests() -> None:
    await demo_buyer_purchase_flow()
    await run_command_logic_test_suite()
    await run_producer_creation_test_suite()
    await demo_producer_future_production_conversation()


# =====================================================================
# PRODUCTEUR — création de produit / déclaration de production future
# =====================================================================
#
# Ces tests exercent la MÊME séquence de nœuds que le graphe compilé pour un
# goal WRITE farm-critical (validator → producer_context_resolver →
# ensure_farm_node → confirmation_gate → mcp_tool_executor → final_response),
# sans passer par `ainvoke`/le LLM de l'interpréteur (non déterministe,
# coûteux) : la même approche que `demo_buyer_purchase_flow` ci-dessus,
# étendue côté producteur. Couvre en un seul passage : validation +
# contrats Pydantic (Phase 2), auto-provisioning de ferme, confirmation
# explicite, dispatch registry → domain service → outil MCP, et rendu
# SUCCESS (Phase 3, `nodes/rendering/`).


async def _run_producer_write_flow(
    runtime: "DemoRuntime",
    *,
    goal: str,
    payload: Dict[str, Any],
    phone: str = COMMAND_TEST_PHONE,
) -> Dict[str, Any]:
    """Rejoue le chemin WRITE du graphe producteur pour `goal`, sans LLM.

    Retourne l'état final accumulé (après confirmation + exécution +
    composition de la réponse) pour assertion par l'appelant.
    """
    from agriconnect.graphs.agents.market_coach.flows.producer.flow import (
        producer_context_resolver,
    )

    state: Dict[str, Any] = {
        "user_phone": phone,
        "user_role": "PRODUCER",
        "current_goal": goal,
        "interpreted_event": "NEW_TASK",
        "transaction_payload": dict(payload),
        "working_memory": {},
    }

    # 1. validator — complétude INTENT_CONFIG.required + contrats Phase 2.
    v = await validator(state, runtime)
    state.update(v)
    if state.get("status") == "WAITING_INPUT":
        return state  # champ manquant/invalide — l'appelant fait l'assertion

    # 2. context_resolver (producer) — auto-résolution farm_id / IDs différés.
    r = await producer_context_resolver(state, runtime)
    state.update(r)
    if str(state.get("status") or "").upper() in {
        "WAITING_INPUT",
        "ERROR",
        "COMPLETED",
    }:
        return state

    # 3. ensure_farm_node — filet WRITE (no-op si farm_id déjà résolu à l'étape 2).
    f = await ensure_farm_node(state, runtime)
    state.update(f)
    if str(state.get("status") or "").upper() == "WAITING_INPUT":
        return state

    # 4. confirmation_gate — 1er passage : pose le récapitulatif, attend CONFIRM.
    c1 = await confirmation_gate(state, runtime)
    state.update(c1)
    assert state.get("status") == "WAITING_CONFIRMATION", (
        f"confirmation_gate attendu en attente, obtenu: {state.get('status')}"
    )

    # 5. Simule la réponse utilisateur "oui" → 2e passage : autorise l'exécution.
    state["interpreted_event"] = "CONFIRM"
    c2 = await confirmation_gate(state, runtime)
    state.update(c2)
    assert state.get("status") == "EXECUTING", (
        f"confirmation_gate attendu EXECUTING après CONFIRM, obtenu: {state.get('status')}"
    )

    # 6. mcp_tool_executor — dispatch registry → domain service → outil MCP.
    e = await mcp_tool_executor(state, runtime)
    state.update(e)

    # 7. final_response — rendu (Phase 3, nodes/rendering/).
    resp = await final_response(state, runtime)
    state.update(resp)
    return state


async def demo_producer_publish_product_flow() -> Dict[str, Any]:
    """SALES_PUBLISH_PRODUCT : publication d'un produit déjà en stock."""
    runtime = DemoRuntime()
    return await _run_producer_write_flow(
        runtime,
        goal="SALES_PUBLISH_PRODUCT",
        payload={"product": "Maïs blanc", "quantity": 50, "unit": "KG", "price": 250},
    )


async def demo_producer_declare_future_production_flow() -> Dict[str, Any]:
    """DECLARE_CROP_CYCLE : déclaration d'une récolte future (préco-commande)."""
    runtime = DemoRuntime()
    return await _run_producer_write_flow(
        runtime,
        goal="DECLARE_CROP_CYCLE",
        payload={
            "product": "Sésame",
            "production_type": "CROP",
            "quantity": 200,
            "unit": "KG",
            "price": 300,
            "estimated_available_at": "2026-09-01",
        },
    )


async def run_producer_creation_test_suite() -> None:
    """Valide bout-en-bout la création de produit et de production future."""
    print("\n=== TEST PRODUCTEUR — création produit / production future ===")

    publish_state = await demo_producer_publish_product_flow()
    assert publish_state.get("status") == "COMPLETED", (
        f"SALES_PUBLISH_PRODUCT: status={publish_state.get('status')} "
        f"errors={publish_state.get('validation_errors')}"
    )
    assert publish_state.get("response_strategy") == "SUCCESS"
    assert publish_state.get("selected_tool") == "create_product"
    assert publish_state.get("transaction_payload") == {}, (
        "payload doit être purgé après succès"
    )
    print(
        f"[SALES_PUBLISH_PRODUCT] ✅ tool={publish_state.get('selected_tool')} "
        f"réponse={publish_state.get('final_response')!r}"
    )

    future_state = await demo_producer_declare_future_production_flow()
    assert future_state.get("status") == "COMPLETED", (
        f"DECLARE_CROP_CYCLE: status={future_state.get('status')} "
        f"errors={future_state.get('validation_errors')}"
    )
    assert future_state.get("response_strategy") == "SUCCESS"
    assert future_state.get("selected_tool") == "declare_future_production"
    # `declare_future_production` prend un `payload` imbriqué (voir domain/agro.py
    # ::AgronomyService.declare_crop_cycle) — farm_id y est injecté par
    # producer_context_resolver._resolve_default_farm, PAS au 1er niveau des args.
    future_tool_payload = (future_state.get("selected_tool_args") or {}).get(
        "payload"
    ) or {}
    assert future_tool_payload.get("farm_id") == "farm-demo-01", (
        "farm_id doit être auto-résolu (1 seule ferme) sans intervention utilisateur, "
        f"obtenu tool_args={future_state.get('selected_tool_args')}"
    )
    print(
        f"[DECLARE_CROP_CYCLE] ✅ tool={future_state.get('selected_tool')} "
        f"réponse={future_state.get('final_response')!r}"
    )

    # ── Garde-fou négatif : le contrat Pydantic (Phase 2) doit rejeter un
    # prix nul AVANT tout appel réseau — filet anti-hallucination LLM.
    runtime = DemoRuntime()
    invalid_state = await _run_producer_write_flow(
        runtime,
        goal="SALES_PUBLISH_PRODUCT",
        payload={"product": "Maïs blanc", "quantity": 50, "unit": "KG", "price": 0},
    )
    assert invalid_state.get("status") == "WAITING_INPUT", (
        f"prix=0 doit être rejeté par le contrat, obtenu: {invalid_state.get('status')}"
    )
    assert "price" in (invalid_state.get("missing_fields") or []), invalid_state.get(
        "missing_fields"
    )
    print(
        f"[SALES_PUBLISH_PRODUCT/prix invalide] ✅ rejeté par le contrat Pydantic "
        f"(re-demande: {invalid_state.get('missing_fields')})"
    )

    print("✅ Logique de création produit + production future validée bout-en-bout.")


# =====================================================================
# TRANSCRIPT LISIBLE — déclaration de production future (revue UX)
# =====================================================================
#
# Contrairement à `run_producer_creation_test_suite` (assertions, payload
# complet dès le départ), cette fonction simule un VRAI dialogue tour par
# tour et IMPRIME chaque message généré par l'agent — questions de coaching
# (LLM réel si `GROQ_API_KEY` est disponible dans l'environnement, sinon
# fallback statique visible tel quel), récapitulatif de confirmation, et
# message de succès. Objectif : relecture humaine de l'UX (ton, concision,
# français Burkina Faso), pas une assertion automatique.


class _LiveLLMDemoRuntime(DemoRuntime):
    """DemoRuntime + accès au VRAI client LLM (Groq) pour voir les questions
    de coaching réellement générées, tout en gardant `call_db` stubbé (aucun
    appel réseau vers MCP/DB)."""

    def __init__(self) -> None:
        self._llm_cache: Any = None
        self._llm_tried = False

    @property
    def llm(self) -> Any:
        if not self._llm_tried:
            self._llm_tried = True
            try:
                from agriconnect.core.get_llm import get_llm

                self._llm_cache = get_llm()
            except Exception as exc:
                print(
                    f"⚠️  LLM indisponible ({exc}) — bascule sur les réponses de secours statiques."
                )
                self._llm_cache = None
        return self._llm_cache

    @property
    def model_answer(self) -> str:
        return "llama-3.1-8b-instant"


# (texte utilisateur, champs qu'il vient de fournir) — ordre volontairement
# naturel (pas l'ordre `field_priority` du validator) pour vérifier que
# l'agent redemande bien SEULEMENT ce qui manque, dans un ordre cohérent.
_FUTURE_PRODUCTION_TURNS = [
    (
        "Bonjour, je vais avoir une récolte de sésame dans quelques mois",
        {"product": "Sésame"},
    ),
    ("Environ 200 kg je pense", {"quantity": 200, "unit": "KG"}),
    ("Je compte vendre ça à 300 FCFA le kilo", {"price": 300}),
    ("C'est une culture, pas de l'élevage", {"production_type": "CROP"}),
    ("Ce sera prêt début septembre 2026", {"estimated_available_at": "2026-09-01"}),
]


async def _turn_boundary(state: Dict[str, Any], runtime: Any) -> Dict[str, Any]:
    """Rejoue la fin de tour réelle du graphe : state_cleaner → final_response
    → post_response_cleanup (edges `response_strategy → state_cleaner →
    final_response → post_response_cleanup → END`).

    Indispensable entre deux tours simulés : `final_response`/`ag_ui_component`
    sont des champs EPHEMERAL (`core/state_profile.py`) remis à None par
    `post_response_cleanup` — sans cet appel, le message du tour précédent
    reste dans le state et `render_ask_missing_field` le réutilise tel quel
    au tour suivant (branche "reuse precomputed final_response"), produisant
    une question qui semble figée/répétée alors que le payload a bien avancé.
    """
    sc = await state_cleaner_node(state, runtime)
    state.update(sc)
    resp = await final_response(state, runtime)
    state.update(resp)
    print(f"🤖 Agent      : {state.get('final_response')}")
    prc = await post_response_cleanup(state, runtime)
    state.update(prc)
    return state


async def demo_producer_future_production_conversation() -> None:
    """Transcript tour par tour de DECLARE_CROP_CYCLE — pour revue UX humaine."""
    print("\n" + "=" * 70)
    print("=== TRANSCRIPT — Déclaration d'une production future (culture) ===")
    print("=" * 70)

    from agriconnect.graphs.agents.market_coach.flows.producer.flow import (
        producer_context_resolver,
    )

    runtime = _LiveLLMDemoRuntime()
    state: Dict[str, Any] = {
        "user_phone": COMMAND_TEST_PHONE,
        "user_role": "PRODUCER",
        "user_name": "Adama",
        "current_goal": "DECLARE_CROP_CYCLE",
        "interpreted_event": "NEW_TASK",
        "transaction_payload": {},
        "working_memory": {},
    }
    accumulated: Dict[str, Any] = {}

    for user_text, new_fields in _FUTURE_PRODUCTION_TURNS:
        accumulated.update(new_fields)
        state["transaction_payload"] = dict(accumulated)
        # current_goal est DURABLE mais reste tout de même réaffirmé ici :
        # dans le vrai graphe c'est goal_planner qui le maintient verrouillé
        # pendant le tunnel (voir [[market-coach-turn-boundary-state]]).
        state["current_goal"] = "DECLARE_CROP_CYCLE"
        # NOTE : plus besoin de reset manuel de final_response/ag_ui_component/
        # response_strategy ici — post_response_cleanup (appelé par
        # _turn_boundary à la fin du tour précédent) s'en charge maintenant
        # lui-même (voir nodes/cleanup.py::CLEANABLE_AFTER_RESPONSE), en plus
        # du reset de secours fait par input_normalizer en tout début de vrai
        # tour. Les deux filets sont désormais alignés.
        print(f"\n🧑 Producteur : {user_text}")

        v = await validator(state, runtime)
        state.update(v)

        if state.get("status") != "WAITING_INPUT":
            # Tous les champs sont réunis — sortir de la boucle de collecte.
            break

        state["response_strategy"] = "ASK_MISSING_FIELD"
        await _turn_boundary(state, runtime)

    # ── Récapitulatif de confirmation ──────────────────────────────────
    r = await producer_context_resolver(state, runtime)
    state.update(r)
    f = await ensure_farm_node(state, runtime)
    state.update(f)
    c1 = await confirmation_gate(state, runtime)
    state.update(c1)
    await _turn_boundary(state, runtime)

    # ── Confirmation utilisateur + exécution ───────────────────────────
    print("\n🧑 Producteur : Oui c'est bon, confirme")
    # post_response_cleanup a effacé current_goal (EPHEMERAL — normalement
    # restauré par goal_planner depuis working_memory.locked_intent en début
    # de tour réel ; ce harnais court-circuite goal_planner, donc on le
    # rétablit ici à la main).
    state["current_goal"] = "DECLARE_CROP_CYCLE"
    state["interpreted_event"] = "CONFIRM"
    c2 = await confirmation_gate(state, runtime)
    state.update(c2)
    e = await mcp_tool_executor(state, runtime)
    state.update(e)
    # Capturé AVANT _turn_boundary : `selected_tool` est EPHEMERAL, remis à
    # None par post_response_cleanup à la fin de CE tour (comportement normal
    # — ce champ n'est utile qu'à mcp_tool_executor/final_response du tour où
    # l'exécution a eu lieu).
    executed_tool = state.get("selected_tool")
    executed_status = state.get("status")
    executed_strategy = state.get("response_strategy")
    await _turn_boundary(state, runtime)

    print("\n" + "-" * 70)
    print(
        f"Statut final : {executed_status} | stratégie : {executed_strategy} "
        f"| outil exécuté : {executed_tool}"
    )
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_manual_smoke_tests())
