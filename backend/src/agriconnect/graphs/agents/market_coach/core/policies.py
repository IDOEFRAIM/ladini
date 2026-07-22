"""Routing Policies — stratégies injectables pour le graphe.

Ne contient plus que la ``FastPathPolicy`` (bypass de la chaîne cognitive).
Le routage post-validator vit dans ``core/router.py::DomainRouter.decide``
(fusion Phase 1 de l'ex-``AfterValidatorPolicy``) ; les ensembles de goals
sont dérivés d'INTENT_CONFIG dans ``core/goals.py``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List

from agriconnect.graphs.agents.market_coach.core.goals import ALL_BUYER_TUNNEL_GOALS

logger = logging.getLogger("AgriConnect.Market.Policies")


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
        return cls(rules=[
            FastPathRule(
                events=frozenset({"ANSWER", "SELECTION"}),
                goals=ALL_BUYER_TUNNEL_GOALS,
            ),
        ])

    @classmethod
    def for_producer(cls) -> "FastPathPolicy":
        return cls(rules=[])


def get_fast_path_policy(role: str) -> FastPathPolicy:
    if str(role).upper().strip() == "BUYER":
        return FastPathPolicy.for_buyer()
    return FastPathPolicy.for_producer()


__all__ = [
    "FastPathRule",
    "FastPathPolicy",
    "get_fast_path_policy",
]
