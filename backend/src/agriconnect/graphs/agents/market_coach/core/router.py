"""Domain Router — Point d'aiguillage rôle-agnostique pour le graphe MarketCoach.

Le ``graph_builder`` ne contient plus de conditions métier ``if role == BUYER``.
Il instancie un ``DefaultDomainRouter(role)`` et branche un unique nœud
``domain_router_node`` qui délègue au bon flow (buyer ou producer) via cet
objet.

Architecture anti-circularité :
  ``core/router.py`` importe ``flows.common.menu_contracts`` (feuille pure).
  Les flows ``buyer/flow`` et ``producer/flow`` sont importés LOCALEMENT
  dans ``DefaultDomainRouter.resolve()`` pour casser le cycle
  ``core → flows → core``.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from agriconnect.graphs.agents.market_coach.flows.common.menu_contracts import (
    DomainResult,
    MenuRequest,
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
# BUYER TRANSACTIONAL GOALS — les goals du tunnel transactionnel
# acheteur qui requièrent un aiguillage spécifique vers les sous-nodes
# cart_management / negotiation_gate AVANT le context_resolver.
# =====================================================================

BUYER_CART_GOALS = frozenset({"BUYER_ADD_TO_CART", "BUYER_VIEW_CART"})
BUYER_PREORDER_GOALS = frozenset({"BUYER_PREORDER_INIT", "BUYER_PREORDER_CONFIRM", "BUYER_CART_RESET"})
BUYER_NEGOTIATION_GOALS = frozenset({"BUYER_NEGOTIATE_PRICE"})
BUYER_ORDER_TRACKING_GOALS = frozenset({
    "BUYER_CHECK_ORDER_STATUS",
    "BUYER_LIST_ORDERS",
    "BUYER_CANCEL_ORDER",
})
BUYER_AUCTION_TRACKING_GOALS = frozenset({
    "BUYER_LIST_AUCTIONS",
    "BUYER_CHECK_AUCTION_STATUS",
})


# =====================================================================
# DEFAULT DOMAIN ROUTER
# =====================================================================

class DefaultDomainRouter:
    """Implémentation concrète du routeur de domaine."""

    __slots__ = ("_role",)

    def __init__(self, role: str ) -> None:
        self._role = str(role).upper().strip()

    @property
    def role(self) -> str:
        return self._role

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

    # ----------------------------------------------------------------
    # ROUTE AFTER VALIDATOR : routage déterministe post-validateur
    # ----------------------------------------------------------------

    def route_after_validator(self, state: Dict[str, Any]) -> str:
        """Routage enrichi post-validateur — injecte les branches
        transactionnelles buyer si le rôle est BUYER.

        Règles :
        1. ERROR / WAITING_INPUT → to_strategy (réponse directe à l'utilisateur)
        2. BUYER + cart goal + PROCESSING → to_cart
        3. BUYER + negotiation goal → to_negotiation
        4. BUYER + order tracking goal → to_order_tracking
        5. Sinon : base router (confirmation / resolver / strategy)
        """
        status = str(state.get("status") or "").upper()

        if self._role == "BUYER":
            working_memory = state.get("working_memory") or {}
            goal = str(
                state.get("current_goal")
                or working_memory.get("active_goal")
                or working_memory.get("locked_intent")
                or state.get("detected_intent")
                or ""
            ).upper()

            # Règle 2 : Cart goals → cart_management SEULEMENT si aucun champ
            # requis ne manque encore (TunnelManager).  Quand le validateur est
            # en WAITING_INPUT avec des champs manquants, response_strategy doit
            # demander l'info — on ne force PAS le passage vers cart_management.
            if goal in BUYER_CART_GOALS:
                missing = list(state.get("missing_fields") or [])
                if not tunnel_manager.is_cart_routeable(status, missing):
                    logger.info(
                        "[DomainRouter] Cart routing blocked by TunnelManager "
                        "(status=%s, missing=%s) → to_strategy",
                        status, missing,
                    )
                    return "to_strategy"
                logger.info(
                    "[DomainRouter] Routing cart intent to cart node (goal=%s, status=%s)",
                    goal, status,
                )
                return "to_cart"
            if goal in BUYER_NEGOTIATION_GOALS:
                if not tunnel_manager.is_negotiation_routeable(status):
                    return "to_strategy"
                return "to_negotiation"
            if goal in BUYER_ORDER_TRACKING_GOALS or goal in BUYER_AUCTION_TRACKING_GOALS:
                return "to_order_tracking"
            if goal in BUYER_PREORDER_GOALS:
                return "to_resolver"

        # Règle 1 : Si erreur ou demande d'input, on répond à l'utilisateur.
        # Cela empêche les boucles : quand validator demande un champ manquant,
        # on ne renvoie PAS vers un node transactionnel (hors forcing cart ci-dessus).
        if status in {"ERROR", "WAITING_INPUT"}:
            return "to_strategy"

        from agriconnect.graphs.agents.market_coach.interpreter.routing import (
            make_route_after_validator,
        )

        base_router = make_route_after_validator(self._role)
        return base_router(state)

    # ----------------------------------------------------------------
    # PRIVATE — résolutions par domaine (imports locaux)
    # ----------------------------------------------------------------

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


def _has_minimum_cart_payload(state: Dict[str, Any]) -> bool:
    payload = state.get("transaction_payload") or {}
    if not isinstance(payload, dict):
        return False
    has_product = bool(payload.get("product"))
    qty = payload.get("quantity")
    has_qty = qty not in (None, "", [], {})
    return has_product or has_qty


__all__ = [
    "DomainResolver",
    "DefaultDomainRouter",
    "DomainResult",
    "BUYER_CART_GOALS",
    "BUYER_PREORDER_GOALS",
    "BUYER_NEGOTIATION_GOALS",
    "BUYER_ORDER_TRACKING_GOALS",
    "BUYER_AUCTION_TRACKING_GOALS",
]