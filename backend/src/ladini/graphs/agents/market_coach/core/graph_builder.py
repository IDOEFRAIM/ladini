"""Market — Graph Builder (factory commune Producer / Buyer).

Assemble le StateGraph LangGraph à partir des nœuds partagés (`shared_core`),
de l'interpréteur role-aware (`interpreter_routing`) et du Context Resolver
spécifique au rôle (`producer_flow` ou `buyer_flow`).

Architecture du graphe :
    input_normalizer → security_moderation
      ├── BLOCK/RESTRICT → response_strategy
      └── ALLOW → session_bootstrap
                    ├── UNAVAILABLE → response_strategy
                    ├── ONBOARDING → onboarding_node
                    └── READY → input_interpreter
                                  ├── to_memory_fast (FastPathPolicy) → memory_update
                                  └── to_cognitive → cognitive_guard
                                        ├── CLARIFY/recover/abandon → clarification_node
                                        │                               ├── réponse terminale → response_strategy
                                        │                               └── no-op → goal_planner
                                        ├── DISAMBIGUATE → semantic_disambiguation
                                        │                    ├── menu construit → response_strategy
                                        │                    └── no-op (compat) → goal_planner
                                        └── START_OR_PLAN_GOAL/CONTINUE_ACTIVE_GOAL/
                                            INTERRUPT_ACTIVE_GOAL → goal_planner
    → goal_planner → memory_update → validator
    → context_resolver → confirmation_gate → mcp_tool_executor
    → response_strategy → final_response → END

(2026-09-08, refonte responsabilités des nœuds d'entrée) :
    - `role_guard` a été SUPPRIMÉ comme nœud — le rôle de profil ne décide
      plus jamais du domaine métier courant (double-rôle : un utilisateur
      peut acheter ET vendre dans la même conversation). Son unique
      responsabilité résiduelle (rôle par défaut) est reprise par
      `session_bootstrap`, avec `graphs.roles.normalize_role` qui ne
      retourne plus jamais "PRODUCER" par défaut pour une valeur inconnue.
    - `session_bootstrap` est un nœud technique (pas une "philosophie" de
      dialogue) qui amorce la session (profil/onboarding/fermes) — voir sa
      docstring pour la justification de son existence et la dette
      assumée qu'il représente.
    - `cognitive_orchestrator` a été SUPPRIMÉ comme nœud — sa
      classification (`phase`/`next_step`/`reason`) ne pilotait AUCUNE
      transition réelle du graphe (preuve dans l'historique de
      `nodes/cognitive.py`) ; c'était une 2e source de vérité décorative.

(2026-09-08, correction topologique du bloc d'entrée) : `session_bootstrap`
a été déplacé APRÈS `security_moderation` (et non plus avant
`input_normalizer`) — un profil/contexte utilisateur ne doit jamais être
chargé avant que l'entrée soit canonicalisée et jugée sûre. Nouvel ordre :
`input_normalizer → security_moderation → [ALLOW] → session_bootstrap`.
`session_bootstrap` est désormais lui-même un nœud décisionnel (voir
`nodes/routing.py::_route_after_session_bootstrap`) : un nouvel
utilisateur (`ONBOARDING`) est routé directement vers `onboarding_node`
SANS jamais passer par `input_interpreter`/`cognitive_guard` (0 appel LLM
inutile) ; un profil indisponible (`UNAVAILABLE`) court-circuite vers
`response_strategy` au même titre qu'un blocage de sécurité.

(2026-09-08, correction topologique du bloc CONVERSATIONNEL,
`cognitive_guard`→`clarification_node`→`semantic_disambiguation`) :
`cognitive_guard` est désormais le PROPRIÉTAIRE UNIQUE de la décision de
transition du tour (`ConversationDecision`, champ `cognitive_decision` —
voir sa docstring pour le contrat complet et la liste exhaustive des
actions). AVANT ce correctif, un edge FIXE envoyait CHAQUE tour — y
compris les plus nominaux ("je veux vendre 2 tonnes de maïs", confiance
0.97) — visiter `clarification_node` PUIS `semantic_disambiguation` avant
d'atteindre enfin `goal_planner`, chacun recalculant SA PROPRE version de
"faut-il intervenir ?" depuis l'état brut. `clarification_node` et
`semantic_disambiguation` sont maintenant des EXÉCUTEURS : ils ne sont
atteints que lorsque `cognitive_guard` a explicitement décidé CLARIFY (ou
`recover_active_tunnel`/`abandon_tunnel_max_retries`) ou DISAMBIGUATE —
jamais pour un tour nominal, qui va directement à `goal_planner`.

Le `clarification_node` choisit seulement QUEL message produire (GPS
déterministe / panne technique déterministe / LLM pédagogique) — jamais SI
un message est dû. Le `semantic_disambiguation` construit le menu depuis le
candidat précalculé par `cognitive_guard` (`disambiguation_candidate`) —
il ne redécide plus lui-même si l'intention est ambiguë.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any, Dict, List, Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from ladini.graphs.agents.market_coach.core.goals import BUYER_CART_GOALS
from ladini.graphs.agents.market_coach.core.policies import get_fast_path_policy
from ladini.graphs.agents.market_coach.core.router import get_domain_router
from ladini.graphs.agents.market_coach.core.state import MarketAgentState
from ladini.graphs.agents.market_coach.flows.buyer.flow import (
    cart_management,
    negotiation_gate,
)
from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import (
    order_tracking_resolver,
)
from ladini.graphs.agents.market_coach.flows.buyer.procurement_execution_finalizer import (
    finalize_procurement_execution,
)
from ladini.graphs.agents.market_coach.flows.common.onboarding import (
    onboarding_node,
)
from ladini.graphs.agents.market_coach.flows.producer.farm_logic import (
    ensure_farm_node,
)
from ladini.graphs.agents.market_coach.flows.producer.sales_execution_finalizer import (
    finalize_sales_publish_execution,
)
from ladini.graphs.agents.market_coach.interpreter.routing import (
    goal_planner,
    make_input_interpreter,
)
from ladini.graphs.agents.market_coach.interpreter.strategy import (
    response_strategy,
)
from ladini.graphs.agents.market_coach.nodes.clarification import (
    clarification_node,
)
from ladini.graphs.agents.market_coach.nodes.cleaner import state_cleaner_node
from ladini.graphs.agents.market_coach.nodes.cleanup import post_response_cleanup
from ladini.graphs.agents.market_coach.nodes.cognitive import cognitive_guard
from ladini.graphs.agents.market_coach.nodes.confirmation_gate import (
    confirmation_gate,
)
from ladini.graphs.agents.market_coach.nodes.executor import mcp_tool_executor
from ladini.graphs.agents.market_coach.nodes.input_normalizer import (
    input_normalizer,
)
from ladini.graphs.agents.market_coach.nodes.memory import memory_update
from ladini.graphs.agents.market_coach.nodes.response_handlers import (
    final_response,
)
from ladini.graphs.agents.market_coach.nodes.routing import (
    _route_after_cognitive_guard,
    _route_after_confirmation,
    _route_after_mcp_executor,
    _route_after_resolver,
    _route_after_security,
    _route_after_session_bootstrap,
)
from ladini.graphs.agents.market_coach.nodes.security_moderation import (
    security_moderation,
)
from ladini.graphs.agents.market_coach.nodes.semantic_disambiguation import (
    semantic_disambiguation,
)
from ladini.graphs.agents.market_coach.nodes.session_bootstrap import (
    session_bootstrap,
)
from ladini.graphs.agents.market_coach.nodes.ui_engine import ui_engine
from ladini.graphs.agents.market_coach.nodes.validation import validator
from ladini.graphs.agents.market_coach.utils import (
    MarketRuntime,
    _safe_node,
    build_runtime,
    build_runtime_from_session,
)
from ladini.graphs.roles import normalize_role

logger = logging.getLogger("Ladini.Market.GraphBuilder")

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
    """Routage post-clarification (2026-09-08, clôture Bloc 1, mandat
    §16/§28/§29).

    `clarification_node` n'est plus atteint QUE par 2 actions :
    - CLARIFY : ce nœud décide seul s'il produit un message (GPS
      déterministe / panne technique / LLM pédagogique) ;
    - ABANDON_ACTIVE_GOAL : `response_strategy` est déjà posé à
      "CLARIFICATION" par `reset_abandoned_conversation_context` AVANT que
      ce nœud ne s'exécute — il ne fait qu'éventuellement l'ENRICHIR d'un
      message LLM contextualisé.

    (RECOVER_ACTIVE_GOAL ne passe plus par ici du tout — voir
    `nodes/routing.py::_COGNITIVE_ACTION_ROUTES`, routé directement vers
    `response_strategy`.)

    Un seul signal suffit donc désormais : `response_strategy=="CLARIFICATION"`
    couvre les 3 sous-cas (CLARIFY réussi, ABANDON avec message LLM, ABANDON
    SANS message LLM — le reset l'a déjà posé). Absence de ce signal ==
    `clarification_node` n'a rien produit pour un CLARIFY générique (LLM
    indisponible, aucun cas GPS/technique) : le tour continue vers
    `goal_planner`, comme avant ce correctif."""
    strategy = str(state.get("response_strategy") or "").upper()
    if strategy == "CLARIFICATION":
        return "to_strategy"
    return "to_planner"


def _route_after_farm_guard(state: MarketAgentState) -> str:
    """P0-2 (audit architectural 2026-09-08) : `ensure_farm_node` a 6 issues
    distinctes (no-op, succès silencieux, sélection multi-fermes,
    clarification READ-sans-ferme, refus d'auto-provisioning, échec de
    création) mais un edge FIXE vers `confirmation_gate` les traitait toutes
    de façon identique — écrasant `final_response`/`pending_interaction`
    dès qu'une décision utilisateur (menu, clarification) venait d'être
    posée. Preuve : `flows/producer/farm_logic.py::_extract_farm_id` +
    branche multi-fermes existaient déjà, correctement écrites — la
    topologie seule les rendait inatteignables.

    Discriminant : `ensure_farm_node` ne pose `final_response` QUE quand il
    a lui-même tranché le tour (les 4 issues bloquantes) — jamais sur le
    no-op ({}) ni sur le succès silencieux (farm_id résolu, juste
    transaction_payload/stable_entities mis à jour). C'est un signal plus
    fiable qu'un `if goal in FARM_CRITICAL_GOALS` dupliqué ici : ce nœud
    reste l'UNIQUE propriétaire de sa propre décision.
    """
    if not state.get("final_response"):
        # No-op ou farm_id résolu silencieusement — poursuit vers la
        # confirmation, comportement inchangé pour le cas nominal.
        return "to_confirmation"
    working = state.get("working_memory") or {}
    if working.get("available_mapping_kind") == "farm":
        # Sélection multi-fermes : passe par ui_engine comme tout autre menu
        # (pass-through sûr en l'absence de `pending_menu` — voir sa
        # docstring — ce nœud ne construit pas de MenuRequest ici, mais la
        # convergence topologique reste correcte et cohérente avec les
        # autres producteurs de menu du graphe).
        return "to_ui"
    # Clarification READ-sans-ferme, refus d'auto-provisioning, échec de
    # création : le tour est déjà conclu par ce nœud, direction le rendu.
    return "to_response"


def _make_route_after_interpreter():
    """Conditional router after input_interpreter.

    Delegates to FastPathPolicy — new tunnel goals are added to the policy
    registry in core/policies.py, not here. Refonte double-rôle : le graphe
    est désormais unique (tunnels acheteur toujours présents), donc la
    politique la plus permissive (`for_buyer`, superset strict de
    `for_producer`) s'applique inconditionnellement.

    (2026-09-08, correction topologique du bloc d'entrée, mandat §14) : ce
    routeur recevait encore un paramètre `role` — mort depuis la refonte
    double-rôle (ce docstring l'expliquait déjà avant ce correctif) : AUCUN
    appelant ni branche ne le lit, `get_fast_path_policy` est appelé en dur
    avec `"BUYER"`. Paramètre et argument d'appel retirés localement (seul
    site concerné, `core/graph_builder.py`) — pas un comportement nouveau,
    juste une dépendance morte de moins.
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

    (2026-09-08, correction topologique du bloc d'entrée, mandat §15) : la
    topologie compilée pour PRODUCER et pour BUYER est désormais
    STRICTEMENT identique (refonte double-rôle : plus aucun edge/nœud
    conditionné par `role_up` dans ce fichier — `_make_route_after_
    interpreter` vient d'ailleurs de perdre son dernier paramètre `role`
    mort, voir sa docstring). Compiler DEUX graphes par rôle n'a donc plus
    de justification structurelle connue — dette probable, pas démantelée
    ici (hors périmètre de cette correction, §17 du mandat : ce chantier ne
    touche que le bloc d'entrée). `role_up` reste néanmoins consommé par
    `make_input_interpreter(role_up)`/`get_domain_router(role_up)` (hors
    périmètre audité), donc pas encore prouvé totalement mort au niveau du
    graphe entier — seulement au niveau de CE fichier.
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
        ("session_bootstrap", session_bootstrap),
        ("input_normalizer", input_normalizer),
        ("security_moderation", security_moderation),
        ("input_interpreter", input_interpreter),
        ("cognitive_guard", cognitive_guard),
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
        ("procurement_execution_finalizer", finalize_procurement_execution),
        ("sales_execution_finalizer", finalize_sales_publish_execution),
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

    workflow.set_entry_point("input_normalizer")

    # Liens du graphe de dialogue
    # (2026-09-08, correction topologique du bloc d'entrée) : `input_normalizer`
    # ne doit plus jamais être décisionnel métier — il canonicalise l'entrée
    # AVANT toute décision de sécurité ou d'amorçage de session. Cet edge
    # reste fixe : voir sa docstring de module.
    workflow.add_edge("input_normalizer", "security_moderation")

    # `security_moderation` ne route plus DIRECTEMENT vers `input_interpreter`
    # sur ALLOW : une entrée autorisée doit d'abord passer par
    # `session_bootstrap` (chargement profil/onboarding, voir
    # `_route_after_session_bootstrap`) avant d'atteindre l'interpréteur.
    # `security_moderation` reste un routeur trivial (Invariant C) — seule sa
    # cible ALLOW change de nom.
    workflow.add_conditional_edges(
        "security_moderation",
        _route_after_security,
        {"to_bootstrap": "session_bootstrap", "to_strategy": "response_strategy"},
    )

    # `session_bootstrap` est désormais un vrai nœud décisionnel (plus un
    # simple relais fixe vers `input_normalizer`, qui s'exécute maintenant
    # EN AMONT) : profil indisponible -> réponse d'erreur déjà préparée par
    # le nœud ; onboarding requis -> `onboarding_node` SANS jamais passer
    # par `input_interpreter`/`cognitive_guard` (aucun appel LLM inutile
    # pour un nouvel utilisateur) ; sinon -> `input_interpreter` nominal.
    workflow.add_conditional_edges(
        "session_bootstrap",
        _route_after_session_bootstrap,
        {
            "to_interpreter": "input_interpreter",
            "to_onboarding": "onboarding_node",
            "to_strategy": "response_strategy",
        },
    )

    route_after_interpreter = _make_route_after_interpreter()
    workflow.add_conditional_edges(
        "input_interpreter",
        route_after_interpreter,
        {"to_cognitive": "cognitive_guard", "to_memory_fast": "memory_update"},
    )

    # (2026-09-08, correction topologique du bloc CONVERSATIONNEL) :
    # `cognitive_guard` est désormais le PROPRIÉTAIRE UNIQUE de la décision
    # de transition du tour — voir sa docstring pour le contrat complet
    # `ConversationDecision` et la liste exhaustive des actions possibles.
    # Le routeur (`_route_after_cognitive_guard`, `nodes/routing.py`) reste
    # TRIVIAL : une lecture de `cognitive_decision.action`, un mapping
    # direct, aucune reclassification. La branche onboarding qui vivait ICI
    # AVANT le correctif du bloc d'ENTRÉE (2026-09-08, plus tôt le même
    # jour) reste retirée pour la même raison qu'alors : `session_bootstrap`
    # tranche cette décision bien plus en amont.
    workflow.add_conditional_edges(
        "cognitive_guard",
        _route_after_cognitive_guard,
        {
            "to_planner": "goal_planner",
            "to_disambiguation": "semantic_disambiguation",
            "to_clarification": "clarification_node",
            # (2026-09-08, clôture Bloc 1, mandat §29) : RECOVER_ACTIVE_GOAL
            # route directement vers response_strategy — clarification_node
            # est un no-op structurel prouvé pour cette action (voir
            # `nodes/clarification.py` + son test dédié) : response_strategy
            # construit déjà toute la réponse RECOVERY depuis
            # `cognitive_action` seul.
            "to_strategy": "response_strategy",
        },
    )

    # (2026-09-08, correction topologique du bloc conversationnel, mandat
    # §16/§28) : `to_disambiguation` RETIRÉ de ce mapping — `cognitive_guard`
    # route désormais DIRECTEMENT vers `semantic_disambiguation` quand il
    # décide DISAMBIGUATE (edge ci-dessus). `clarification_node` n'a donc
    # plus que deux issues réelles : une réponse terminale (CLARIFY réussi,
    # ou ABANDON avec message LLM), ou — repli non-nominal si le LLM échoue
    # pendant un ABANDON (voir `_route_after_clarification`) — la poursuite
    # du flow vers `goal_planner`.
    workflow.add_conditional_edges(
        "clarification_node",
        _route_after_clarification,
        {
            "to_planner": "goal_planner",
            "to_strategy": "response_strategy",
        },
    )

    # (2026-09-08, clôture Bloc 1, mandat §24) : edge FIXE — `to_planner` a
    # été RETIRÉ. Preuve : `semantic_disambiguation` n'est atteint QUE via
    # `cognitive_guard --DISAMBIGUATE-->`, et cette décision exige déjà
    # `disambiguation_candidate` présent avec ≥2 options valides (voir
    # `nodes/cognitive.py::_classify_nominal_action`) — le no-op de
    # `semantic_disambiguation` (repli de compatibilité épuisé) est donc
    # structurellement INATTEIGNABLE depuis le graphe compilé nominal. S'il
    # se produisait quand même (violation de contrat, checkpoint corrompu),
    # `semantic_disambiguation` le journalise désormais comme CONTRACT
    # VIOLATION (voir sa docstring) et son patch vide traverse
    # `response_strategy`, qui applique son propre repli générique — jamais
    # un crash, jamais un aiguillage silencieux vers `goal_planner` non plus.
    workflow.add_edge("semantic_disambiguation", "response_strategy")

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

    # (2026-09-08, P2-1 audit architectural) : "to_strategy" -> "to_ui" —
    # ce label menait déjà à `ui_engine`, jamais `response_strategy` ; le
    # nom trompait la lecture du graphe (logs, diagnostic, maintenance).
    workflow.add_conditional_edges(
        "context_resolver",
        _route_after_resolver,
        {
            "to_confirmation": "confirmation_gate",
            "to_ui": "ui_engine",
            "to_farm_guard": "ensure_farm_node",
        },
    )

    # ui_engine transforme pending_menu → ag_ui_component puis passe à response_strategy
    workflow.add_edge("ui_engine", "response_strategy")

    # (2026-09-08, P0-2) : edge fixe remplacé — voir _route_after_farm_guard.
    workflow.add_conditional_edges(
        "ensure_farm_node",
        _route_after_farm_guard,
        {
            "to_confirmation": "confirmation_gate",
            "to_ui": "ui_engine",
            "to_response": "response_strategy",
        },
    )

    workflow.add_conditional_edges(
        "confirmation_gate",
        _route_after_confirmation,
        {"to_executor": "mcp_tool_executor", "to_strategy": "response_strategy"},
    )

    # (2026-09-08, P2-2 audit architectural) : `_route_after_executor` ne
    # retournait qu'UNE seule valeur possible (`"to_strategy"`) — un
    # conditional edge à une seule issue n'est sémantiquement qu'un
    # `add_edge`, avec le coût cognitif d'un branchement en plus. Fonction
    # supprimée (voir nodes/routing.py).
    #
    # (2026-09-08, P2-3 audit architectural) : le chaînage no-op
    # `mcp_tool_executor → procurement_finalizer → sales_finalizer →
    # response_strategy` (chaque finalizer se protégeant par son propre
    # garde interne "draft absent -> {}") est remplacé par une VRAIE
    # sélection au niveau du routeur — `_route_after_mcp_executor` (voir
    # nodes/routing.py) — sur le même signal que chaque garde interne
    # utilisait déjà (présence de `procurement_draft`/`sales_publish_draft`,
    # ni l'un ni l'autre touché par `mcp_tool_executor`). Un draft ne peut
    # être QUE PROCUREMENT ou SALES pour un tour donné (goals disjoints) :
    # au plus UN finalizer s'exécute par tour, les deux convergent ensuite
    # vers response_strategy.
    workflow.add_conditional_edges(
        "mcp_tool_executor",
        _route_after_mcp_executor,
        {
            "to_procurement_finalizer": "procurement_execution_finalizer",
            "to_sales_finalizer": "sales_execution_finalizer",
            "to_response": "response_strategy",
        },
    )
    workflow.add_edge("procurement_execution_finalizer", "response_strategy")
    workflow.add_edge("sales_execution_finalizer", "response_strategy")

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

    from ladini.graphs.agents.market_coach.flows.buyer.flow import (
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

    from ladini.graphs.agents.market_coach.flows.buyer.flow import _create_preorder

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
    text: Optional[str] = None,
    said: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Rejoue le chemin WRITE du graphe producteur pour `goal`, sans LLM.

    `text`/`said` : le message utilisateur simulé et les entités « dites » de ce tour. Le modèle
    commercial (SALES_PUBLISH_PRODUCT, Phase B1) exige une PROVENANCE réelle pour la base du prix :
    un `price_unit` absent du texte serait « inféré » et refusé.

    Retourne l'état final accumulé (après confirmation + exécution +
    composition de la réponse) pour assertion par l'appelant.
    """
    from ladini.graphs.agents.market_coach.flows.producer.flow import (
        producer_context_resolver,
    )

    state: Dict[str, Any] = {
        "user_phone": phone,
        "user_role": "PRODUCER",
        "current_goal": goal,
        "interpreted_event": "NEW_TASK",
        "transaction_payload": dict(payload),
        "working_memory": {},
        "normalized_text": text or "",
        "extracted_entities": dict(said or {}),
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
    # `WAITING_CONFIRMATION` (mécanisme générique confirmation_summary,
    # goals pas encore migrés — ex: PRODUCTION_DECLARE_FUTURE) ou `WAITING_INPUT`
    # (draft canonique versionné — SALES_PUBLISH_PRODUCT depuis 2026-09-04,
    # PROCUREMENT_CREATE_REQUEST depuis 2026-09-03, voir
    # `nodes/confirmation_gate.py::_DRAFT_BASED_CONFIRMATION_GOALS`) sont
    # TOUS LES DEUX des issues valides "en attente de confirmation" — la
    # valeur exacte dépend de l'état de migration de CE goal précis, pas
    # une régression.
    assert state.get("status") in {"WAITING_CONFIRMATION", "WAITING_INPUT"}, (
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
        payload={
            "product": "Maïs blanc",
            "quantity": 50,
            "unit": "KG",
            "price": 250,
            "price_unit": "KG",
        },
        text="50 kg de maïs blanc à 250 FCFA le kg",
        said={"quantity": 50, "unit": "KG", "price": 250, "price_unit": "KG"},
    )


async def demo_producer_declare_future_production_flow() -> Dict[str, Any]:
    """PRODUCTION_DECLARE_FUTURE : déclaration d'une récolte future (préco-commande)."""
    runtime = DemoRuntime()
    return await _run_producer_write_flow(
        runtime,
        goal="PRODUCTION_DECLARE_FUTURE",
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
        f"PRODUCTION_DECLARE_FUTURE: status={future_state.get('status')} "
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
        f"[PRODUCTION_DECLARE_FUTURE] ✅ tool={future_state.get('selected_tool')} "
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
                from ladini.core.get_llm import get_llm

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
    """Transcript tour par tour de PRODUCTION_DECLARE_FUTURE — pour revue UX humaine."""
    print("\n" + "=" * 70)
    print("=== TRANSCRIPT — Déclaration d'une production future (culture) ===")
    print("=" * 70)

    from ladini.graphs.agents.market_coach.flows.producer.flow import (
        producer_context_resolver,
    )

    runtime = _LiveLLMDemoRuntime()
    state: Dict[str, Any] = {
        "user_phone": COMMAND_TEST_PHONE,
        "user_role": "PRODUCER",
        "user_name": "Adama",
        "current_goal": "PRODUCTION_DECLARE_FUTURE",
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
        state["current_goal"] = "PRODUCTION_DECLARE_FUTURE"
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
    # restauré par goal_planner depuis working_memory.active_goal en début
    # de tour réel ; ce harnais court-circuite goal_planner, donc on le
    # rétablit ici à la main).
    state["current_goal"] = "PRODUCTION_DECLARE_FUTURE"
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
