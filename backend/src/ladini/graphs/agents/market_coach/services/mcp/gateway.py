"""MCP domain gateways — typed wrappers around ``mc_runtime.call_db``.

Each gateway encapsulates a single MCP domain (profile, farm, auction, …),
providing explicit method signatures instead of raw ``call_db(tool_name, **kw)``
calls scattered across the codebase.

All cross-cutting concerns (None-stripping, ASCII folding, ensure_dict,
request_id, logging) live in ``MarketRuntime.call_db`` — gateways are thin
typed pass-throughs that add MCPCallError translation.

Usage::

    gw = ProfileGateway(mc_runtime)
    profile = await gw.get_user_by_phone("+226…")
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ladini.graphs.agents.market_coach.services.mcp.error_translation import (
    classify_error,
    translate_mcp_error,
)

logger = logging.getLogger("Ladini.Market.MCPGateway")


class MCPCallError(Exception):
    """Raised when an MCP gateway call fails."""

    def __init__(self, tool: str, message: str, error_code: str, request_id: str):
        self.tool = tool
        self.error_code = error_code
        self.request_id = request_id
        super().__init__(message)


class _BaseGateway:
    __slots__ = ("_rt",)

    def __init__(self, mc_runtime: Any) -> None:
        self._rt = mc_runtime

    async def _call(self, tool: str, **kwargs: Any) -> Dict[str, Any]:
        try:
            return await self._rt.call_db(tool, **kwargs)
        except MCPCallError:
            raise
        except Exception as exc:
            error_code = classify_error(str(exc))
            raise MCPCallError(
                tool=tool,
                message=translate_mcp_error(str(exc)),
                error_code=error_code,
                request_id="n/a",
            ) from exc


# ── Profile ────────────────────────────────────────────────────────


class ProfileGateway(_BaseGateway):
    async def get_user_by_phone(self, phone: str) -> Dict[str, Any]:
        result = await self._call("get_user_by_phone", phone=phone.strip())
        logger.debug("ProfileGateway.get_user_by_phone status=%s", result.get("status"))
        return result

    async def identify_or_create_user(self, phone: str) -> Dict[str, Any]:
        return await self._call("identify_or_create_user", phone=phone.strip())


# ── Farm ───────────────────────────────────────────────────────────


class FarmGateway(_BaseGateway):
    async def list_farms(self, phone: str) -> List[Dict[str, Any]]:
        result = await self._call("get_producer_farm", phone=phone.strip())
        farms = result.get("data") or result.get("farms") or result.get("results") or []
        return farms if isinstance(farms, list) else []

    async def list_farms_alt(self, phone: str) -> List[Dict[str, Any]]:
        """Alternative endpoint used by producer flow (``get_farms``)."""
        result = await self._call("get_farms", phone=phone.strip())
        farms = result.get("data") or []
        return farms if isinstance(farms, list) else []

    async def create_farm(self, **kwargs: Any) -> Dict[str, Any]:
        result = await self._call("create_farm", **kwargs)
        return result.get("data") or result

    async def get_or_create_farm(
        self,
        phone: str,
        farm_name: str | None = None,
        zone_id: str | None = None,
    ) -> Dict[str, Any]:
        """Idempotent : renvoie la ferme existante ou en crée une (crée aussi le
        profil producteur si absent). Outil role-allowed via l'intent FARM_CREATE
        (`tool_name=get_or_create_farm`), contrairement à `create_farm`.
        """
        kwargs: Dict[str, Any] = {"phone": phone.strip()}
        if farm_name:
            kwargs["farm_name"] = farm_name
        if zone_id:
            kwargs["zone_id"] = zone_id
        result = await self._call("get_or_create_farm", **kwargs)
        return result.get("data") or result

    async def get_offer_reservations(
        self, phone: str, market_offer_id: str | None = None
    ) -> Dict[str, Any]:
        """Précommandes reçues par le producteur sur ses productions futures."""
        kwargs: Dict[str, Any] = {"phone": phone.strip()}
        if market_offer_id:
            kwargs["market_offer_id"] = market_offer_id
        return await self._call("get_offer_reservations", **kwargs)


# ── Auctions & Bids ───────────────────────────────────────────────


class AuctionGateway(_BaseGateway):
    async def search_auctions(self, **kwargs: Any) -> Dict[str, Any]:
        return await self._call("get_auctions", **kwargs)

    search_open_auctions = search_auctions

    async def get_producer_auctions(
        self,
        phone: str,
        scope: str = "MATCHABLE",
        product_name: str | None = None,
        zone_name: str | None = None,
    ) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"phone": phone.strip(), "scope": scope}
        if product_name:
            kwargs["product_name"] = product_name
        if zone_name:
            kwargs["zone_name"] = zone_name
        return await self._call("get_producer_auctions", **kwargs)

    async def place_bid(
        self,
        auction_id: str,
        phone: str,
        offered_price: Any,
        message: str | None = None,
        *,
        price_basis: str | None = None,
        price_unit: str | None = None,
        package_type: str | None = None,
        package_content_amount: Any = None,
        package_content_unit: str | None = None,
    ) -> Dict[str, Any]:
        # Phase B2b : un nouveau bid porte OBLIGATOIREMENT sa base (`price_basis`) — sans elle le serveur
        # refuse (`price_basis_required`). `_call` retire les `None`.
        return await self._call(
            "place_bid",
            auction_id=auction_id,
            phone=phone.strip(),
            offered_price=offered_price,
            message=message,
            price_basis=price_basis,
            price_unit=price_unit,
            package_type=package_type,
            package_content_amount=package_content_amount,
            package_content_unit=package_content_unit,
        )

    async def get_my_active_bids(self, phone: str) -> Dict[str, Any]:
        return await self._call("get_my_active_bids", phone=phone.strip())

    async def update_auction(
        self, phone: str, auction_id: str, **fields: Any
    ) -> Dict[str, Any]:
        """Mise à jour partielle d'un appel d'offres acheteur (quantité,
        unité, prix plafond, date limite)."""
        return await self._call(
            "update_auction_fields",
            phone=phone.strip(),
            auction_id=auction_id,
            **fields,
        )

    async def update_bid_price(
        self,
        bid_id: str,
        phone: str,
        new_price: Any,
        *,
        price_basis: str | None = None,
        price_unit: str | None = None,
        package_type: str | None = None,
        package_content_amount: Any = None,
        package_content_unit: str | None = None,
    ) -> Dict[str, Any]:
        return await self._call(
            "update_bid_price",
            bid_id=bid_id,
            phone=phone.strip(),
            new_price=new_price,
            price_basis=price_basis,
            price_unit=price_unit,
            package_type=package_type,
            package_content_amount=package_content_amount,
            package_content_unit=package_content_unit,
        )

    async def get_auctions_bids(self, **kwargs: Any) -> Dict[str, Any]:
        return await self._call("get_auctions_bids", **kwargs)

    async def get_auction_bids(
        self, auction_id: str, phone: str | None = None
    ) -> Dict[str, Any]:
        # phone requis pour l'identité de contexte MCP (sinon PermissionDenied
        # « missing_context_identity »). _call strip les None automatiquement.
        return await self._call("get_auction_bids", auction_id=auction_id, phone=phone)

    async def select_winning_bid(
        self,
        bid_id: str,
        phone: str | None = None,
        delivery_lat: float | None = None,
        delivery_lon: float | None = None,
        idempotency_key: str | None = None,
        expected_award: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        # `expected_award` (Phase B2b) : les termes CONFIRMÉS par l'acheteur (empreinte de la décision
        # certifiée), revalidés côté serveur sous verrou avant d'attribuer.
        # phone requis pour l'identité de contexte MCP (sinon PermissionDenied).
        # `idempotency_key` (2026-09-17, follow-up pre-Hetzner) : accepter
        # une transition d'enchère GAGNANTE crée un `Order` — une action
        # irréversible, money-adjacent, explicitement listée comme
        # prioritaire. Voir `select_winning_bid_key()` ci-dessous pour la
        # dérivation ; `None` (par défaut) préserve le comportement
        # antérieur pour tout appelant qui ne le fournit pas encore.
        return await self._call(
            "select_winning_bid",
            bid_id=bid_id,
            phone=phone,
            delivery_lat=delivery_lat,
            delivery_lon=delivery_lon,
            idempotency_key=idempotency_key,
            expected_award=expected_award,
        )


# ── Stock ──────────────────────────────────────────────────────────


class StockGateway(_BaseGateway):
    async def get_producer_stocks(self, phone: str) -> Dict[str, Any]:
        return await self._call("get_producer_stocks", phone=phone.strip())

    async def list_productions(self, phone: str) -> Dict[str, Any]:
        """Liste aplatie des productions futures / lots (MarketOffer) du producteur."""
        return await self._call("list_producer_productions", phone=phone.strip())

    async def update_production(
        self, phone: str, cycle_id: str, **fields: Any
    ) -> Dict[str, Any]:
        """Mise à jour partielle d'un lot (prix/quantité/nom/unité/date/type)."""
        return await self._call(
            "update_production_fields", phone=phone.strip(), cycle_id=cycle_id, **fields
        )

    async def validate_stock_availability(
        self,
        product_id: str,
        quantity: float,
        unit: str,
        buyer_phone: str,
        tier_id: Optional[str] = None,
        package_count: Optional[int] = None,
    ) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "product_id": product_id, "quantity": quantity, "unit": unit, "buyer_phone": buyer_phone,
        }
        if tier_id and package_count:
            # B16 : un conditionnement se vérifie PAR VARIANTE (compte de conditionnements disponibles).
            kwargs["tier_id"] = str(tier_id)
            kwargs["package_count"] = int(package_count)
        return await self._call("validate_stock_availability_atomic", **kwargs)


# ── Product Search ─────────────────────────────────────────────────


class ProductGateway(_BaseGateway):
    async def search_products(self, product: str, phone: str) -> Dict[str, Any]:
        return await self._call("search_products", product=product, phone=phone)

    async def get_my_products(self, phone: str) -> Dict[str, Any]:
        return await self._call("get_my_products", phone=phone.strip())

    async def update_product(
        self, phone: str, product_id: str, **fields: Any
    ) -> Dict[str, Any]:
        """Mise à jour partielle d'un produit catalogue (prix/quantité/nom/unité)."""
        return await self._call(
            "update_product_price_and_qty",
            phone=phone.strip(),
            product_id=product_id,
            **fields,
        )

    # (2026-09-04, Product Completeness Phase 2) : `delete_product` existait
    # déjà côté DB, complète et sûre (verrou FOR UPDATE, contrôle de
    # propriété, refus si commandes actives, archivage doux si historique de
    # commandes, suppression physique sinon) — mais AUCUN chemin
    # conversationnel ne l'atteignait : un producteur ne pouvait donc jamais
    # retirer un produit de son catalogue. Ce wrapper est la seule pièce qui
    # manquait côté agent.
    async def delete_product(self, phone: str, product_id: str) -> Dict[str, Any]:
        return await self._call(
            "delete_product", phone=phone.strip(), product_id=product_id
        )


# ── Negotiation ────────────────────────────────────────────────────


class NegotiationGateway(_BaseGateway):
    async def initiate_session(
        self,
        buyer_phone: str,
        product_id: str,
        offered_price: Any,
        quantity: Any,
    ) -> Dict[str, Any]:
        return await self._call(
            "initiate_negotiation_session",
            buyer_phone=buyer_phone,
            product_id=product_id,
            offered_price=offered_price,
            quantity=quantity,
        )

    async def update_offer(
        self, buyer_phone: str, negotiation_id: str, new_price: Any
    ) -> Dict[str, Any]:
        return await self._call(
            "update_negotiation_offer",
            buyer_phone=buyer_phone,
            negotiation_id=negotiation_id,
            new_price=new_price,
        )

    async def close_session(
        self, buyer_phone: str, negotiation_id: str, reason: str = "buyer_abandoned"
    ) -> Dict[str, Any]:
        return await self._call(
            "close_negotiation_session",
            buyer_phone=buyer_phone,
            negotiation_id=negotiation_id,
            reason=reason,
        )


# ── Preorder ───────────────────────────────────────────────────────


class PreorderGateway(_BaseGateway):
    async def create_draft(
        self,
        buyer_phone: str,
        cart_items: Any,
        payment_method: str = "CASH",
        delivery_zone_id: Any = None,
        idempotency_key: Any = None,
    ) -> Dict[str, Any]:
        # (2026-09-03, migration PREORDER, mandat §15) : clé d'idempotence
        # CLIENT — réutilise `mcp_idempotency_store` (RÉELLEMENT dédupliqué
        # côté serveur depuis le chantier PROCUREMENT, pas une abstraction
        # de façade) pour qu'un retry avec le MÊME panier/acheteur ne crée
        # pas un second `Order(status=DRAFT)`.
        return await self._call(
            "create_preorder_draft",
            buyer_phone=buyer_phone,
            cart_items=cart_items,
            payment_method=payment_method,
            delivery_zone_id=delivery_zone_id,
            idempotency_key=idempotency_key,
        )

    async def confirm_draft(
        self,
        buyer_phone: str,
        preorder_id: str,
        delivery_lat: Any = None,
        delivery_lon: Any = None,
        idempotency_key: Any = None,
    ) -> Dict[str, Any]:
        return await self._call(
            "confirm_preorder_draft",
            buyer_phone=buyer_phone,
            preorder_id=preorder_id,
            delivery_lat=delivery_lat,
            delivery_lon=delivery_lon,
            idempotency_key=idempotency_key,
        )

    async def cancel_draft(
        self,
        buyer_phone: str,
        preorder_id: str,
        reason: Any = None,
        target_status: str = "CANCELLED",
    ) -> Dict[str, Any]:
        # (2026-09-04, audit Order(DRAFT) orphelin PREORDER) : ferme
        # l'`Order` Postgres sous-jacent en même temps que le `PreorderDraft`
        # applicatif — `target_status="SUPERSEDED"` pour le cycle "ajouter
        # d'autres produits" (voir `cancel_preorder_draft`, services/database/buyer.py).
        return await self._call(
            "cancel_preorder_draft",
            buyer_phone=buyer_phone,
            preorder_id=preorder_id,
            reason=reason,
            target_status=target_status,
        )

    async def reserve_future_offer(
        self,
        buyer_phone: str,
        market_offer_id: str,
        quantity: Any,
        desired_price: Any = None,
    ) -> Dict[str, Any]:
        return await self._call(
            "reserve_future_offer",
            buyer_phone=buyer_phone.strip(),
            market_offer_id=market_offer_id,
            quantity=quantity,
            desired_price=desired_price,
        )


# ── Order Tracking ─────────────────────────────────────────────────


class OrderTrackingGateway(_BaseGateway):
    async def get_transaction_summary(self, **kwargs: Any) -> Dict[str, Any]:
        return await self._call("get_transaction_summary", **kwargs)

    async def get_buyer_orders_dashboard(self, phone: str) -> Dict[str, Any]:
        return await self._call("get_buyer_orders_dashboard", phone=phone.strip())

    async def cancel_pending_order(
        self, order_id: str, phone: str, reason: str = ""
    ) -> Dict[str, Any]:
        return await self._call(
            "cancel_pending_order",
            order_id=order_id,
            phone=phone,
            reason=reason,
        )

    # (2026-09-04, clôture F1 — paiement à la livraison) : lecture "mes
    # commandes" côté PRODUCTEUR — déjà auditée/corrigée séparément
    # (exclusion DRAFT/SUPERSEDED, inclusion des commandes RFQ), réutilisée
    # ici telle quelle pour résoudre QUELLE commande le producteur vise
    # (jamais "la dernière", voir `flows/producer/flow.py::_resolve_order_for_delivery_payment`).
    async def get_producer_orders(self, phone: str, status: str = "") -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"phone": phone.strip()}
        if status:
            kwargs["status"] = status
        return await self._call("get_producer_orders", **kwargs)

    # (2026-09-04, Phase 5 — décision produit #1) : symétrique producteur de
    # `cancel_pending_order` côté acheteur.
    async def cancel_confirmed_order(
        self, producer_phone: str, order_id: str, reason: str = ""
    ) -> Dict[str, Any]:
        return await self._call(
            "cancel_confirmed_order",
            producer_phone=producer_phone.strip(),
            order_id=order_id,
            reason=reason,
        )

    # (2026-09-13, confirmation explicite producteur) : miroir en écriture
    # de `cancel_confirmed_order` ci-dessus.
    async def confirm_order_by_producer(
        self, producer_phone: str, order_id: str
    ) -> Dict[str, Any]:
        return await self._call(
            "confirm_order_by_producer",
            producer_phone=producer_phone.strip(),
            order_id=order_id,
        )

    async def confirm_delivery_and_payment(
        self, producer_phone: str, order_id: str
    ) -> Dict[str, Any]:
        return await self._call(
            "confirm_delivery_and_payment",
            producer_phone=producer_phone.strip(),
            order_id=order_id,
        )


# ── Moderation / Anti-abuse ────────────────────────────────────────


class ModerationGateway(_BaseGateway):
    async def get_account_status(self, phone: str) -> Dict[str, Any]:
        return await self._call("get_account_status", phone=phone.strip())

    async def get_last_interactive_outbound(self, phone: str) -> Dict[str, Any]:
        """B20 — dernier message proactif interactif (attend une réponse nue) envoyé à `phone`."""
        return await self._call("get_last_interactive_outbound", phone=phone.strip())

    async def get_prohibited_terms(self) -> Dict[str, Any]:
        return await self._call("get_prohibited_terms")

    async def record_moderation_strike(
        self,
        phone: str,
        matched_term: str = "",
        excerpt: str = "",
        kind: str = "PROHIBITED_PRODUCT",
    ) -> Dict[str, Any]:
        return await self._call(
            "record_moderation_strike",
            phone=phone.strip(),
            matched_term=matched_term,
            excerpt=excerpt,
            kind=kind,
        )

    async def record_demand_signal(
        self,
        phone: str = "",
        raw_query: str = "",
        normalized_term: str = "",
        zone_id: Any = None,
    ) -> Dict[str, Any]:
        return await self._call(
            "record_demand_signal",
            phone=phone,
            raw_query=raw_query,
            normalized_term=normalized_term,
            zone_id=zone_id,
        )


# ── Agent Actions ──────────────────────────────────────────────────


class AgentActionGateway(_BaseGateway):
    async def create_action(
        self, agent_name: str, action_type: str, payload: Any
    ) -> Dict[str, Any]:
        return await self._call(
            "create_agent_action",
            agent_name=agent_name,
            action_type=action_type,
            payload=payload,
        )


# ── Escrow (Paydunya) ──────────────────────────────────────────────


class EscrowGateway(_BaseGateway):
    async def initiate_escrow_payment(
        self,
        buyer_phone: str,
        preorder_id: str,
        delivery_lat: Optional[float] = None,
        delivery_lon: Optional[float] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        args: Dict[str, Any] = {
            "buyer_phone": buyer_phone.strip(),
            "preorder_id": preorder_id,
        }
        if delivery_lat is not None and delivery_lon is not None:
            args["delivery_lat"] = delivery_lat
            args["delivery_lon"] = delivery_lon
        if idempotency_key is not None:
            args["idempotency_key"] = idempotency_key
        return await self._call("initiate_escrow_payment", **args)

    async def verify_delivery_otp(
        self, producer_phone: str, otp_code: str
    ) -> Dict[str, Any]:
        return await self._call(
            "verify_delivery_otp",
            producer_phone=producer_phone.strip(),
            otp_code=otp_code,
        )

    async def list_escrowed_orders(self, producer_phone: str) -> Dict[str, Any]:
        return await self._call(
            "list_producer_escrowed_orders", producer_phone=producer_phone.strip()
        )


class RecurringSupplyGateway(_BaseGateway):
    """Approvisionnement récurrent (Phase 2) — `services/database/recurring_supply.py`."""

    async def create_recurring_need(
        self,
        phone: str,
        product_query: str,
        quantity: float,
        unit: str,
        recurrence_type: str,
        weekly_days: Any = None,
        excluded_weekdays: Any = None,
        starts_at: Any = None,
        ends_at: Any = None,
        max_price_per_unit: Any = None,
        idempotency_key: Any = None,
        draft_id: Any = None,
        draft_version: Any = None,
    ) -> Dict[str, Any]:
        return await self._call(
            "create_recurring_need",
            phone=phone,
            product_query=product_query,
            quantity=quantity,
            unit=unit,
            recurrence_type=recurrence_type,
            weekly_days=weekly_days,
            excluded_weekdays=excluded_weekdays,
            starts_at=starts_at,
            ends_at=ends_at,
            max_price_per_unit=max_price_per_unit,
            idempotency_key=idempotency_key,
            draft_id=draft_id,
            draft_version=draft_version,
        )

    async def create_recurring_needs(
        self,
        phone: str,
        items: Any,
        recurrence_type: str,
        weekly_days: Any = None,
        excluded_weekdays: Any = None,
        starts_at: Any = None,
        ends_at: Any = None,
        max_price_per_unit: Any = None,
        idempotency_key: Any = None,
        draft_id: Any = None,
        draft_version: Any = None,
    ) -> Dict[str, Any]:
        """Variante plurielle (chantier multi-produits, 2026-09-23) — `items` est une liste de
        `{"product_query", "quantity", "unit"}`, tous créés dans UNE SEULE transaction côté service
        (voir `services/database/recurring_supply.py::create_recurring_needs`)."""
        return await self._call(
            "create_recurring_needs",
            phone=phone,
            items=items,
            recurrence_type=recurrence_type,
            weekly_days=weekly_days,
            excluded_weekdays=excluded_weekdays,
            starts_at=starts_at,
            ends_at=ends_at,
            max_price_per_unit=max_price_per_unit,
            idempotency_key=idempotency_key,
            draft_id=draft_id,
            draft_version=draft_version,
        )

    async def update_recurring_need(
        self,
        phone: str,
        recurring_need_id: str,
        action: str,
        quantity: Any = None,
        recurrence_type: Any = None,
        weekly_days: Any = None,
        excluded_weekdays: Any = None,
        paused_until: Any = None,
        occurrence_date: Any = None,
    ) -> Dict[str, Any]:
        return await self._call(
            "update_recurring_need",
            phone=phone,
            recurring_need_id=recurring_need_id,
            action=action,
            quantity=quantity,
            recurrence_type=recurrence_type,
            weekly_days=weekly_days,
            excluded_weekdays=excluded_weekdays,
            paused_until=paused_until,
            occurrence_date=occurrence_date,
        )

    async def list_my_recurring_needs(self, phone: str) -> Dict[str, Any]:
        return await self._call("list_my_recurring_needs", phone=phone)

    async def get_recurring_need_detail(self, phone: str, recurring_need_id: str) -> Dict[str, Any]:
        return await self._call("get_recurring_need_detail", phone=phone, recurring_need_id=recurring_need_id)

    async def accept_match_proposal(
        self,
        phone: str,
        recurring_need_id: str,
        action: str,
        occurrence_id: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"phone": phone, "recurring_need_id": recurring_need_id, "action": action}
        if occurrence_id is not None:
            # Réponse à UNE proposition précise (digest) — jamais « la prochaine occurrence ouverte ».
            kwargs["occurrence_id"] = occurrence_id
        if expected_version is not None:
            # Version MÉTIER que l'acheteur a vue (digest/écran détail) : garde principale côté service.
            kwargs["expected_version"] = int(expected_version)
        return await self._call("accept_match_proposal", **kwargs)

    async def mark_order_delivery_status(
        self, phone: str, order_id: str, action: str
    ) -> Dict[str, Any]:
        return await self._call(
            "mark_order_delivery_status", phone=phone, order_id=order_id, action=action
        )

    async def list_my_deliverable_orders(self, phone: str, delivery_status: str) -> Dict[str, Any]:
        return await self._call(
            "list_my_deliverable_orders", phone=phone, delivery_status=delivery_status
        )

    async def record_order_reception(
        self,
        phone: str,
        order_id: str,
        outcome: str,
        issue_type: Any = None,
        detail: Any = None,
        received_quantity: Any = None,
    ) -> Dict[str, Any]:
        return await self._call(
            "record_order_reception",
            phone=phone,
            order_id=order_id,
            outcome=outcome,
            issue_type=issue_type,
            detail=detail,
            received_quantity=received_quantity,
        )


__all__ = [
    "MCPCallError",
    "ProfileGateway",
    "FarmGateway",
    "AuctionGateway",
    "StockGateway",
    "ProductGateway",
    "NegotiationGateway",
    "PreorderGateway",
    "OrderTrackingGateway",
    "ModerationGateway",
    "AgentActionGateway",
    "EscrowGateway",
    "RecurringSupplyGateway",
]
