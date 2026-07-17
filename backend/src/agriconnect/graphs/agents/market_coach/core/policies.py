"""Routing Policies — Injectable strategy objects for graph routing decisions.

Replaces hardcoded `if role == BUYER` conditions in graph_builder.py with
composable, testable policy objects. New tunnels (e.g. LOGISTICS) can be
added by registering route rules — no modification to graph_builder needed.

Architecture:
    graph_builder creates policies from a role-specific configuration,
    then passes them to generic conditional-edge functions.

Two policy types:
    AfterValidatorPolicy — decides where to go after the validator node
    FastPathPolicy       — decides whether to skip the cognitive chain
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Protocol

from agriconnect.graphs.agents.market_coach.core.tunnel_manager import tunnel_manager

logger = logging.getLogger("AgriConnect.Market.Policies")


# =====================================================================
# ROUTE RULE — single routing decision
# =====================================================================

@dataclass(frozen=True)
class RouteRule:
    """A single routing decision: if goal matches, route to target.

    Attributes:
        goals:      frozenset of goal strings that trigger this rule
        target:     graph edge label (e.g. "to_cart", "to_negotiation")
        guard:      optional callable(state) -> bool. If provided, the rule
                    only fires when guard returns True. Returning False falls
                    through to the next rule.
    """
    goals: FrozenSet[str]
    target: str
    guard: Optional[Callable[[Dict[str, Any]], bool]] = None


# =====================================================================
# AFTER-VALIDATOR POLICY
# =====================================================================

class AfterValidatorPolicy:
    """Configurable post-validator routing.

    Replaces DefaultDomainRouter.route_after_validator() with a declarative
    list of RouteRules evaluated in order. First match wins.

    Usage:
        policy = AfterValidatorPolicy.for_buyer()
        edge_label = policy.decide(state)
    """

    __slots__ = ("_rules", "_fallback_router")

    def __init__(
        self,
        rules: List[RouteRule],
        fallback_router: Optional[Callable[[Dict[str, Any]], str]] = None,
    ) -> None:
        self._rules = tuple(rules)
        self._fallback_router = fallback_router

    def decide(self, state: Dict[str, Any]) -> str:
        working_memory = state.get("working_memory") or {}
        goal = str(
            state.get("current_goal")
            or working_memory.get("active_goal")
            or working_memory.get("locked_intent")
            or state.get("detected_intent")
            or ""
        ).upper()

        status = str(state.get("status") or "").upper()

        for rule in self._rules:
            if goal not in rule.goals:
                continue
            if rule.guard is not None and not rule.guard(state):
                logger.info(
                    "[Policy] Rule %s blocked by guard (goal=%s, status=%s) → fallthrough",
                    rule.target, goal, status,
                )
                return "to_strategy"
            logger.info(
                "[Policy] Routing to %s (goal=%s, status=%s)",
                rule.target, goal, status,
            )
            return rule.target

        if status in {"ERROR", "WAITING_INPUT"}:
            return "to_strategy"

        if self._fallback_router is not None:
            return self._fallback_router(state)

        return "to_resolver"

    # ── Factory methods ───────────────────────────────────────────

    @classmethod
    def for_buyer(cls) -> "AfterValidatorPolicy":
        from agriconnect.graphs.agents.market_coach.core.router import (
            BUYER_CART_GOALS,
            BUYER_NEGOTIATION_GOALS,
            BUYER_ORDER_TRACKING_GOALS,
            BUYER_AUCTION_TRACKING_GOALS,
            BUYER_PREORDER_GOALS,
        )
        from agriconnect.graphs.agents.market_coach.interpreter.routing import (
            make_route_after_validator,
        )

        def _cart_guard(state: Dict[str, Any]) -> bool:
            status = str(state.get("status") or "").upper()
            missing = list(state.get("missing_fields") or [])
            return tunnel_manager.is_cart_routeable(status, missing)

        def _negotiation_guard(state: Dict[str, Any]) -> bool:
            status = str(state.get("status") or "").upper()
            return tunnel_manager.is_negotiation_routeable(status)

        rules = [
            RouteRule(goals=BUYER_CART_GOALS, target="to_cart", guard=_cart_guard),
            RouteRule(goals=BUYER_NEGOTIATION_GOALS, target="to_negotiation", guard=_negotiation_guard),
            RouteRule(goals=BUYER_ORDER_TRACKING_GOALS | BUYER_AUCTION_TRACKING_GOALS, target="to_order_tracking"),
            RouteRule(goals=BUYER_PREORDER_GOALS, target="to_resolver"),
        ]

        return cls(rules=rules, fallback_router=make_route_after_validator("BUYER"))

    # Intents producteur ENTIÈREMENT pris en charge par producer_auction_resolver
    # (parcours d'enchères, tunnel de bid, suivi/modification d'offres). Ils gèrent
    # eux-mêmes leur propre confirmation via working_memory.bid_phase et ne doivent
    # JAMAIS être routés vers confirmation_gate/mcp_tool_executor : sinon un
    # `price`/`auction_id` résiduel fait croire au pipeline natif à une transaction
    # complète → récap générique parasite + « erreur technique » à la confirmation.
    PRODUCER_RESOLVER_GOALS = frozenset({
        "MARKET_GET_REQUESTS",
        "MARKET_GET_MY_PROPOSALS",
        "SALES_PLACE_BID",
    })

    @classmethod
    def for_producer(cls) -> "AfterValidatorPolicy":
        from agriconnect.graphs.agents.market_coach.interpreter.routing import (
            make_route_after_validator,
        )
        rules = [
            RouteRule(goals=cls.PRODUCER_RESOLVER_GOALS, target="to_resolver"),
        ]
        return cls(rules=rules, fallback_router=make_route_after_validator("PRODUCER"))


# =====================================================================
# FAST-PATH POLICY
# =====================================================================

@dataclass(frozen=True)
class FastPathRule:
    """Events + goals that qualify for cognitive-chain bypass."""
    events: FrozenSet[str]
    goals: FrozenSet[str]


class FastPathPolicy:
    """Decides whether to skip the cognitive chain after input_interpreter.

    When a turn is a simple slot-filling answer (ANSWER/SELECTION) within
    an active buyer tunnel, the cognitive chain (5 nodes) adds latency
    without changing the outcome. This policy short-circuits to memory_update.
    """

    __slots__ = ("_rules",)

    def __init__(self, rules: List[FastPathRule]) -> None:
        self._rules = tuple(rules)

    def should_skip_cognitive(self, state: Dict[str, Any]) -> bool:
        if state.get("active_form"):
            return False

        event = str(state.get("interpreted_event") or "").upper()
        goal = str(state.get("current_goal") or "").upper()

        for rule in self._rules:
            if event in rule.events and goal in rule.goals:
                logger.info(
                    "[FastPath] Skipping cognitive chain → memory_update: event=%s goal=%s",
                    event, goal,
                )
                return True
        return False

    def route(self, state: Dict[str, Any]) -> str:
        if self.should_skip_cognitive(state):
            return "to_memory_fast"
        return "to_cognitive"

    # ── Factory ───────────────────────────────────────────────────

    @classmethod
    def for_buyer(cls) -> "FastPathPolicy":
        from agriconnect.graphs.agents.market_coach.core.router import (
            BUYER_CART_GOALS,
            BUYER_PREORDER_GOALS,
            BUYER_NEGOTIATION_GOALS,
            BUYER_ORDER_TRACKING_GOALS,
            BUYER_AUCTION_TRACKING_GOALS,
        )

        all_buyer_tunnel_goals = (
            BUYER_CART_GOALS
            | BUYER_PREORDER_GOALS
            | BUYER_NEGOTIATION_GOALS
            | BUYER_ORDER_TRACKING_GOALS
            | BUYER_AUCTION_TRACKING_GOALS
        )

        return cls(rules=[
            FastPathRule(
                events=frozenset({"ANSWER", "SELECTION"}),
                goals=all_buyer_tunnel_goals,
            ),
        ])

    @classmethod
    def for_producer(cls) -> "FastPathPolicy":
        return cls(rules=[])


# =====================================================================
# POLICY REGISTRY — role → policies
# =====================================================================

_POLICY_FACTORIES = {
    "BUYER": {
        "after_validator": AfterValidatorPolicy.for_buyer,
        "fast_path": FastPathPolicy.for_buyer,
    },
    "PRODUCER": {
        "after_validator": AfterValidatorPolicy.for_producer,
        "fast_path": FastPathPolicy.for_producer,
    },
}


def get_after_validator_policy(role: str) -> AfterValidatorPolicy:
    role = role.upper().strip()
    factory = _POLICY_FACTORIES.get(role, _POLICY_FACTORIES["PRODUCER"])
    return factory["after_validator"]()


def get_fast_path_policy(role: str) -> FastPathPolicy:
    role = role.upper().strip()
    factory = _POLICY_FACTORIES.get(role, _POLICY_FACTORIES["PRODUCER"])
    return factory["fast_path"]()


__all__ = [
    "RouteRule",
    "AfterValidatorPolicy",
    "FastPathRule",
    "FastPathPolicy",
    "get_after_validator_policy",
    "get_fast_path_policy",
]
