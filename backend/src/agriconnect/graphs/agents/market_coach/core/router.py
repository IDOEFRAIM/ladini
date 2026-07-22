"""Domain Router — aiguillage rôle-agnostique du graphe MarketCoach.

Fusion (Phase 1) de l'ex-``DefaultDomainRouter`` (résolution de contexte) et
de l'ex-``AfterValidatorPolicy`` (routage post-validator, ``policies.py``) :
UNE seule classe porte désormais les deux décisions de routage métier.

    router = get_domain_router(role)
    workflow.add_conditional_edges("validator", router.decide, targets)
    patch = await router.resolve(state, mc_runtime)   # context_resolver

Les ensembles de goals viennent de ``core/goals.py`` (dérivés d'INTENT_CONFIG,
anti-drift) — plus aucune définition locale ici.

Architecture anti-circularité :
  ``core/router.py`` importe ``flows.common.menu_contracts`` (feuille pure).
  Les flows ``buyer/flow`` et ``producer/flow`` sont importés LOCALEMENT
  dans ``resolve()`` pour casser le cycle ``core → flows → core``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, Optional, Protocol, Sequence, runtime_checkable

from agriconnect.graphs.agents.market_coach.flows.common.menu_contracts import (
    DomainResult,
    MenuRequest,
)
from agriconnect.graphs.agents.market_coach.core.goals import (
    BUYER_CART_GOALS,
    BUYER_NEGOTIATION_GOALS,
    BUYER_ORDER_TRACKING_GOALS,
    BUYER_AUCTION_TRACKING_GOALS,
    BUYER_PREORDER_GOALS,
    PRODUCER_RESOLVER_GOALS,
)
from agriconnect.graphs.agents.market_coach.core.tunnel_manager import tunnel_manager

logger = logging.getLogger("AgriConnect.Market.DomainRouter")


# =====================================================================
# PROTOCOL (structural typing — les flows buyer/producer n'ont pas
# besoin d'hériter quoi que ce soit, ils exposent juste `resolve`).
# =====================================================================

@runtime_checkable
class DomainResolver(Protocol):
    """Contrat qu'un résolveur de domaine doit respecter."""

    async def resolve(
        self,
        state: Dict[str, Any],
        mc_runtime: Any,
    ) -> DomainResult: ...


# =====================================================================
# ROUTE RULE — une décision de routage post-validator
# =====================================================================

@dataclass(frozen=True)
class RouteRule:
    """Si le goal courant appartient à ``goals``, router vers ``target``.

    ``guard`` optionnel : si fourni et qu'il retourne False, la règle est
    bloquée et la décision retombe sur ``to_strategy`` (réponse utilisateur).
    """
    goals: FrozenSet[str]
    target: str
    guard: Optional[Callable[[Dict[str, Any]], bool]] = None


def _cart_guard(state: Dict[str, Any]) -> bool:
    status = str(state.get("status") or "").upper()
    missing = list(state.get("missing_fields") or [])
    return tunnel_manager.is_cart_routeable(status, missing)


def _negotiation_guard(state: Dict[str, Any]) -> bool:
    status = str(state.get("status") or "").upper()
    return tunnel_manager.is_negotiation_routeable(status)


# =====================================================================
# DOMAIN ROUTER — resolve() + decide() fusionnés
# =====================================================================

class DomainRouter:
    """Routeur de domaine unifié : contexte + post-validator."""

    __slots__ = ("_role", "_rules", "_fallback_router")

    def __init__(
        self,
        role: str,
        rules: Sequence[RouteRule] = (),
        fallback_router: Optional[Callable[[Dict[str, Any]], str]] = None,
    ) -> None:
        self._role = str(role).upper().strip()
        self._rules = tuple(rules)
        self._fallback_router = fallback_router

    @property
    def role(self) -> str:
        return self._role

    # ----------------------------------------------------------------
    # DECIDE : routage déterministe post-validator (ex-AfterValidatorPolicy)
    # ----------------------------------------------------------------

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
                    "[Router] Rule %s blocked by guard (goal=%s, status=%s) → to_strategy",
                    rule.target, goal, status,
                )
                return "to_strategy"
            logger.info(
                "[Router] Routing to %s (goal=%s, status=%s)",
                rule.target, goal, status,
            )
            return rule.target

        if status in {"ERROR", "WAITING_INPUT"}:
            return "to_strategy"

        if self._fallback_router is not None:
            return self._fallback_router(state)

        return "to_resolver"

    # ----------------------------------------------------------------
    # RESOLVE : délègue au bon flow via import local (anti-cycle)
    # ----------------------------------------------------------------

    async def resolve(
        self,
        state: Dict[str, Any],
        mc_runtime: Any,
    ) -> DomainResult:
        """Appelle ``buyer_context_resolver`` ou ``producer_context_resolver``
        selon le rôle, et encapsule le résultat dans un ``DomainResult``.
        """
        if self._role == "BUYER":
            return await self._resolve_buyer(state, mc_runtime)
        return await self._resolve_producer(state, mc_runtime)

    async def _resolve_buyer(
        self,
        state: Dict[str, Any],
        mc_runtime: Any,
    ) -> DomainResult:
        from agriconnect.graphs.agents.market_coach.flows.buyer.flow import (
            buyer_context_resolver,
        )
        raw = await buyer_context_resolver(state, mc_runtime)
        return _wrap_raw_result(raw)

    async def _resolve_producer(
        self,
        state: Dict[str, Any],
        mc_runtime: Any,
    ) -> DomainResult:
        from agriconnect.graphs.agents.market_coach.flows.producer.flow import (
            producer_context_resolver,
        )
        raw = await producer_context_resolver(state, mc_runtime)
        return _wrap_raw_result(raw)

    # ── Factories par rôle ───────────────────────────────────────────

    @classmethod
    def for_buyer(cls) -> "DomainRouter":
        from agriconnect.graphs.agents.market_coach.interpreter.routing import (
            make_route_after_validator,
        )
        rules = [
            RouteRule(goals=BUYER_CART_GOALS, target="to_cart", guard=_cart_guard),
            RouteRule(goals=BUYER_NEGOTIATION_GOALS, target="to_negotiation", guard=_negotiation_guard),
            RouteRule(goals=BUYER_ORDER_TRACKING_GOALS | BUYER_AUCTION_TRACKING_GOALS, target="to_order_tracking"),
            RouteRule(goals=BUYER_PREORDER_GOALS, target="to_resolver"),
        ]
        return cls("BUYER", rules=rules, fallback_router=make_route_after_validator("BUYER"))

    @classmethod
    def for_producer(cls) -> "DomainRouter":
        from agriconnect.graphs.agents.market_coach.interpreter.routing import (
            make_route_after_validator,
        )
        rules = [
            RouteRule(goals=PRODUCER_RESOLVER_GOALS, target="to_resolver"),
        ]
        return cls("PRODUCER", rules=rules, fallback_router=make_route_after_validator("PRODUCER"))


def get_domain_router(role: str) -> DomainRouter:
    """Factory unique — point d'entrée du graph_builder."""
    if str(role).upper().strip() == "BUYER":
        return DomainRouter.for_buyer()
    return DomainRouter.for_producer()


# =====================================================================
# HELPERS
# =====================================================================

def _extract_menu_from_patch(patch: Dict[str, Any]) -> Optional[MenuRequest]:
    menu = patch.pop("pending_menu", None)
    if isinstance(menu, MenuRequest):
        return menu
    return None


def _wrap_raw_result(raw: Dict[str, Any]) -> DomainResult:
    menu = _extract_menu_from_patch(raw)
    return DomainResult(state_patch=raw, pending_menu=menu)


__all__ = [
    "DomainResolver",
    "DomainRouter",
    "RouteRule",
    "get_domain_router",
    "DomainResult",
    # Re-exports compat (source canonique : core/goals.py)
    "BUYER_CART_GOALS",
    "BUYER_PREORDER_GOALS",
    "BUYER_NEGOTIATION_GOALS",
    "BUYER_ORDER_TRACKING_GOALS",
    "BUYER_AUCTION_TRACKING_GOALS",
]
