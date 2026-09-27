"""``BusinessEventEmitter`` — the ONLY door business-flow code goes through to
record a buyer-journey fact. Centralizes exactly what the mission asked it
to: event_name/journey validation, canonical unit resolution, measurement
family classification, idempotency-key discipline, and JSON-safe metadata
serialization — callers never touch SQL, never recompute a canonical unit,
never invent an idempotency key format.

Usage (inside an existing business transaction, same `session`):

    from ladini.domain.analytics.emitter import BusinessEventEmitter
    from ladini.domain.analytics.business_events import BusinessEventName
    from ladini.domain.analytics.metric_dictionary import Journey

    await BusinessEventEmitter(session).emit(
        event_name=BusinessEventName.DIRECT_ORDER_CREATED,
        journey=Journey.DIRECT,
        actor_type="BUYER",
        actor_id=buyer_user_id,
        buyer_id=buyer_profile_id,
        entity_type="ORDER",
        entity_id=order.id,
        idempotency_key=f"DIRECT_ORDER_CREATED:{order.id}",
        sub_category_id=sub_category_id,
        zone_id=zone_id,
        quantity=quantity, unit=unit,
        amount=amount,
    )

No try/except around this call is needed or wanted (mission: "INTERDIT: try/
except pass comme mécanisme principal") — `emit()` only ever does one cheap,
same-database INSERT into `analytics.event_outbox` (`ON CONFLICT DO NOTHING`
on the idempotency key), in the CALLER's own transaction. If it fails, the
caller's transaction SHOULD roll back — a broken emit() call is a real bug,
not something to paper over. This is also exactly why the design is safe
from being a SPOF: the only external-ish thing this touches is the SAME
Postgres the business write is already in; the outbox->business_events drain
(the part that could theoretically be slow or briefly unavailable) is fully
decoupled into its own async worker, never in this call path.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from ladini.domain.analytics.business_events import BusinessEvent, BusinessEventName
from ladini.domain.analytics.metric_dictionary import Journey
from ladini.domain.analytics.units import convert_to_canonical, measurement_family_of
from ladini.workers.repositories import analytics_outbox_repo


def _json_safe(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Journey) or isinstance(value, BusinessEventName):
        return value.value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return value


class BusinessEventEmitter:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def emit(
        self,
        *,
        event_name: BusinessEventName,
        journey: Journey,
        actor_type: str,
        entity_type: str,
        entity_id: Any,
        idempotency_key: str,
        actor_id: Optional[Any] = None,
        buyer_id: Optional[Any] = None,
        producer_id: Optional[Any] = None,
        category_id: Optional[Any] = None,
        sub_category_id: Optional[Any] = None,
        zone_id: Optional[Any] = None,
        quantity: Optional[float] = None,
        unit: Optional[str] = None,
        canonical_unit_override: Optional[str] = None,
        amount: Optional[float] = None,
        currency: str = "XOF",
        metadata: Optional[Dict[str, Any]] = None,
        occurred_at: Optional[datetime] = None,
    ) -> bool:
        """Validate, canonicalize, and enqueue one business event. Returns
        whether a NEW outbox row was inserted (False = this exact
        idempotency_key was already queued/landed, a normal outcome on
        retry, never an error the caller needs to handle differently).

        Canonical unit resolution: if `unit` is given and no
        `canonical_unit_override`, resolves the canonical unit from
        `resolve_subcategory_canonical_unit` when a SubCategory row is
        available to the caller — but this function never queries the DB
        itself (no N+1 risk, mission §9): pass `canonical_unit_override`
        when the caller already knows the subcategory's canonical unit
        (the common case, since callers already loaded the product/
        subcategory for their own business logic). When no canonical unit
        can be determined, `canonical_quantity`/`canonical_unit` stay NULL
        — never a guessed conversion (mission §8: "si aucune conversion
        fiable n'existe, ne fabrique pas canonical_quantity").
        """
        canonical_quantity: Optional[float] = None
        canonical_unit: Optional[str] = None
        family: Optional[str] = None
        if unit:
            family = measurement_family_of(unit)
            target = canonical_unit_override or unit
            if quantity is not None:
                converted = convert_to_canonical(float(quantity), unit, target)
                if converted is not None:
                    canonical_quantity = converted
                    canonical_unit = target

        event = BusinessEvent(
            event_name=event_name,
            journey=journey,
            actor_type=actor_type,
            actor_id=str(actor_id) if actor_id else None,
            buyer_id=str(buyer_id) if buyer_id else None,
            producer_id=str(producer_id) if producer_id else None,
            entity_type=entity_type,
            entity_id=str(entity_id),
            category_id=str(category_id) if category_id else None,
            sub_category_id=str(sub_category_id) if sub_category_id else None,
            zone_id=str(zone_id) if zone_id else None,
            quantity=float(quantity) if quantity is not None else None,
            unit=unit,
            canonical_quantity=canonical_quantity,
            canonical_unit=canonical_unit,
            measurement_family=family,
            amount=float(amount) if amount is not None else None,
            currency=currency,
            metadata=metadata or {},
            occurred_at=occurred_at or datetime.now(timezone.utc),
            idempotency_key=idempotency_key,
        )

        payload = {k: _json_safe(v) for k, v in asdict(event).items()}
        inserted = await analytics_outbox_repo.enqueue(
            self.session,
            event_name=event.event_name.value,
            journey=event.journey.value,
            payload=payload,
            dedupe_key=idempotency_key,
        )
        return bool(inserted)

    async def emit_order_delivered(self, order: Any) -> bool:
        """Shared by every `Order.delivery_status = "DELIVERED"`/"FULFILLED"
        write site (escrow OTP verification, cash-on-delivery producer
        confirmation; walk-in `record_sale` `FULFILLED` orders have no buyer and are not a buyer-journey event) — see the Phase C DELIVERED-vs-
        FULFILLED decision in the architecture doc (§11.5.1.A) for why this
        is order_type-conditional rather than one flat rule.

        RECURRING_SUPPLY orders are deliberately SKIPPED here: for that flow
        `DELIVERED` is only the producer's intermediate claim, not a buyer-
        confirmed fact (see the same doc section) — `RECURRING_OCCURRENCE_
        DELIVERED` is out of this session's instrumentation scope (mission
        Phase C, step 6, does not list it among the priority events) and is
        documented as NOT INSTRUMENTED rather than wired to the wrong signal.
        """
        order_type = str(getattr(order, "order_type", "") or "").upper()
        if order_type == "RECURRING_SUPPLY":
            return False
        if getattr(order, "market_offer_id", None) is not None:
            return False  # future-production reservation: not part of the DIRECT/TENDER journeys
        is_tender = getattr(order, "auction_id", None) is not None
        event_name = BusinessEventName.TENDER_DELIVERED if is_tender else BusinessEventName.DIRECT_ORDER_DELIVERED
        journey = Journey.TENDER if is_tender else Journey.DIRECT
        return await self.emit(
            event_name=event_name,
            journey=journey,
            actor_type="SYSTEM",
            buyer_id=getattr(order, "buyer_id", None),
            zone_id=getattr(order, "zone_id", None),
            entity_type="ORDER",
            entity_id=order.id,
            idempotency_key=f"{event_name.value}:{order.id}",
            amount=float(order.total_amount) if getattr(order, "total_amount", None) is not None else None,
        )

    # ------------------------------------------------------------------ DIRECT lifecycle
    #
    # The canonical DIRECT buyer flow is `create_preorder_draft` (one Order per producer,
    # order_type=PREORDER, status DRAFT = an unconfirmed cart, NOT a need) ->
    # `confirm_preorder_draft` (buyer confirms: the order leaves DRAFT, stock is debited) ->
    # producer acceptance (`confirm_order_by_producer`) or escrow payment (`mark_escrow_paid`),
    # both writing Order.status = CONFIRMED -> delivery. `finalize_multi_order` is a dead legacy path.

    @staticmethod
    def _is_direct_order(order: Any) -> bool:
        """Buyer catalog order: not a tender order, not a recurring-supply order, not a
        future-production reservation, and owned by a buyer profile."""
        return (
            getattr(order, "auction_id", None) is None
            and str(getattr(order, "order_type", "") or "").upper() != "RECURRING_SUPPLY"
            and getattr(order, "market_offer_id", None) is None
            and getattr(order, "buyer_id", None) is not None
        )

    async def emit_direct_order_created(self, order: Any) -> bool:
        """DIRECT_ORDER_CREATED: the buyer's order became a real need (left DRAFT). Sub-category
        (and quantity/unit for a single untiered line) are taken from the order's items only when
        they are already loaded on the instance — never a lazy load, never a guess."""
        if not self._is_direct_order(order):
            return False
        items = list(order.__dict__.get("items") or [])
        products = [getattr(i, "product", None) for i in items]
        subs = {getattr(p, "sub_category_id", None) for p in products if p is not None}
        sub_category_id = next(iter(subs)) if len(subs) == 1 and None not in subs else None
        single = items[0] if len(items) == 1 and getattr(items[0], "tier_id", None) is None else None
        product = getattr(single, "product", None) if single is not None else None
        return await self.emit(
            event_name=BusinessEventName.DIRECT_ORDER_CREATED,
            journey=Journey.DIRECT,
            actor_type="BUYER",
            buyer_id=order.buyer_id,
            zone_id=getattr(order, "zone_id", None),
            entity_type="ORDER",
            entity_id=order.id,
            idempotency_key=f"DIRECT_ORDER_CREATED:{order.id}",
            sub_category_id=sub_category_id,
            quantity=float(single.quantity) if single is not None and product is not None else None,
            unit=getattr(product, "unit", None) if product is not None else None,
            amount=float(order.total_amount or 0.0),
        )

    async def emit_direct_order_confirmed(self, order: Any, *, actor_type: str) -> bool:
        """DIRECT_ORDER_CONFIRMED: the order reached Order.status = CONFIRMED (producer accepted
        it, or the escrow payment was secured) — the firm, executable commitment that delivery
        requires. Called only AFTER that status is persisted; recurring/tender orders never emit."""
        if not self._is_direct_order(order):
            return False
        return await self.emit(
            event_name=BusinessEventName.DIRECT_ORDER_CONFIRMED,
            journey=Journey.DIRECT,
            actor_type=actor_type,
            buyer_id=order.buyer_id,
            zone_id=getattr(order, "zone_id", None),
            entity_type="ORDER",
            entity_id=order.id,
            idempotency_key=f"DIRECT_ORDER_CONFIRMED:{order.id}",
            amount=float(order.total_amount or 0.0),
        )


__all__ = ["BusinessEventEmitter"]
