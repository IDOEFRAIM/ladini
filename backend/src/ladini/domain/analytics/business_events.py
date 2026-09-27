"""``analytics.business_events`` — CONTRACT ONLY (mission: "PRÉPARATION
SEULEMENT... ne branche pas encore toute l'instrumentation").

No table, no migration, no writer is added in this phase. What's here is the
frozen shape Phase C's outbox-based emitter and the eventual
``analytics.business_events`` table must match, so that phase doesn't have
to redesign the contract under time pressure — see the architecture doc,
section "Business events contract", for why each field exists and which
existing mechanism it reuses (the transactional-outbox *pattern* proven by
``workers/outbox/dispatcher.py``, not the `notification_outbox` table
itself — that table is shaped for delivery channels, not typed facts, per
the Phase A audit).

An event describes a FACT THAT ALREADY HAPPENED (mission: "un événement doit
décrire un FAIT passé" — `RECURRING_DIGEST_ACCEPTED`, never
`PROCESS_DIGEST`). Every name below is a past participle / completed action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from ladini.domain.analytics.metric_dictionary import Journey


class BusinessEventName(str, Enum):
    # DIRECT
    DIRECT_SEARCH_PERFORMED = "DIRECT_SEARCH_PERFORMED"
    DIRECT_SEARCH_SUCCEEDED = "DIRECT_SEARCH_SUCCEEDED"
    DIRECT_ORDER_CREATED = "DIRECT_ORDER_CREATED"
    DIRECT_ORDER_CONFIRMED = "DIRECT_ORDER_CONFIRMED"
    DIRECT_ORDER_DELIVERED = "DIRECT_ORDER_DELIVERED"
    DIRECT_ORDER_FAILED = "DIRECT_ORDER_FAILED"

    # TENDER
    TENDER_CREATED = "TENDER_CREATED"
    TENDER_PUBLISHED = "TENDER_PUBLISHED"
    TENDER_BID_RECEIVED = "TENDER_BID_RECEIVED"
    TENDER_WINNER_SELECTED = "TENDER_WINNER_SELECTED"
    TENDER_ORDER_CREATED = "TENDER_ORDER_CREATED"
    TENDER_DELIVERED = "TENDER_DELIVERED"

    # RECURRING
    RECURRING_NEED_CREATED = "RECURRING_NEED_CREATED"
    RECURRING_OCCURRENCE_CREATED = "RECURRING_OCCURRENCE_CREATED"
    RECURRING_MATCH_FOUND = "RECURRING_MATCH_FOUND"
    RECURRING_DIGEST_SENT = "RECURRING_DIGEST_SENT"
    RECURRING_DIGEST_ACCEPTED = "RECURRING_DIGEST_ACCEPTED"
    RECURRING_DIGEST_MODIFIED = "RECURRING_DIGEST_MODIFIED"
    RECURRING_OCCURRENCE_SKIPPED = "RECURRING_OCCURRENCE_SKIPPED"
    RECURRING_OCCURRENCE_CONFIRMED = "RECURRING_OCCURRENCE_CONFIRMED"
    RECURRING_OCCURRENCE_DELIVERED = "RECURRING_OCCURRENCE_DELIVERED"

    # SUPPLY (Producer Analytics Phase B) — raw facts about a Product's
    # sellable state, never an interpretation of business intent. See
    # docs/analytics/PRODUCER_ANALYTICS_ARCHITECTURE.md §7/§11.
    PRODUCT_PUBLISHED_FOR_SALE = "PRODUCT_PUBLISHED_FOR_SALE"
    PRODUCT_SELLABLE_QUANTITY_CHANGED = "PRODUCT_SELLABLE_QUANTITY_CHANGED"


#: journey + whether quantity/unit fields are expected to be populated for
#: this event — used by the data-quality checks the mission asks for
#: ("event sans entity_id lorsque requis"). TENDER_BID_RECEIVED etc. carry a
#: quantity (the bid volume); DIRECT_SEARCH_PERFORMED does not.
EVENT_JOURNEY: dict[BusinessEventName, Journey] = {
    BusinessEventName.DIRECT_SEARCH_PERFORMED: Journey.DIRECT,
    BusinessEventName.DIRECT_SEARCH_SUCCEEDED: Journey.DIRECT,
    BusinessEventName.DIRECT_ORDER_CREATED: Journey.DIRECT,
    BusinessEventName.DIRECT_ORDER_CONFIRMED: Journey.DIRECT,
    BusinessEventName.DIRECT_ORDER_DELIVERED: Journey.DIRECT,
    BusinessEventName.DIRECT_ORDER_FAILED: Journey.DIRECT,
    BusinessEventName.TENDER_CREATED: Journey.TENDER,
    BusinessEventName.TENDER_PUBLISHED: Journey.TENDER,
    BusinessEventName.TENDER_BID_RECEIVED: Journey.TENDER,
    BusinessEventName.TENDER_WINNER_SELECTED: Journey.TENDER,
    BusinessEventName.TENDER_ORDER_CREATED: Journey.TENDER,
    BusinessEventName.TENDER_DELIVERED: Journey.TENDER,
    BusinessEventName.RECURRING_NEED_CREATED: Journey.RECURRING,
    BusinessEventName.RECURRING_OCCURRENCE_CREATED: Journey.RECURRING,
    BusinessEventName.RECURRING_MATCH_FOUND: Journey.RECURRING,
    BusinessEventName.RECURRING_DIGEST_SENT: Journey.RECURRING,
    BusinessEventName.RECURRING_DIGEST_ACCEPTED: Journey.RECURRING,
    BusinessEventName.RECURRING_DIGEST_MODIFIED: Journey.RECURRING,
    BusinessEventName.RECURRING_OCCURRENCE_SKIPPED: Journey.RECURRING,
    BusinessEventName.RECURRING_OCCURRENCE_CONFIRMED: Journey.RECURRING,
    BusinessEventName.RECURRING_OCCURRENCE_DELIVERED: Journey.RECURRING,
    BusinessEventName.PRODUCT_PUBLISHED_FOR_SALE: Journey.SUPPLY,
    BusinessEventName.PRODUCT_SELLABLE_QUANTITY_CHANGED: Journey.SUPPLY,
}

#: Events that MUST carry quantity+unit (a physical fact, not just a status
#: change) — a data-quality check should flag any row for these names with a
#: NULL quantity as suspect (mission Phase P: "event sans entity_id/quantity
#: lorsque requis").
EVENTS_REQUIRING_QUANTITY = frozenset(
    {
        BusinessEventName.TENDER_CREATED,
        BusinessEventName.TENDER_BID_RECEIVED,
        BusinessEventName.RECURRING_OCCURRENCE_CREATED,
        BusinessEventName.RECURRING_MATCH_FOUND,
        BusinessEventName.RECURRING_OCCURRENCE_CONFIRMED,
        BusinessEventName.RECURRING_OCCURRENCE_DELIVERED,
        BusinessEventName.PRODUCT_PUBLISHED_FOR_SALE,
        BusinessEventName.PRODUCT_SELLABLE_QUANTITY_CHANGED,
    }
)

#: Events that MUST carry an amount (a monetary fact) — DIRECT/TENDER order
#: events and the winner-selection event, where a price is already known.
EVENTS_REQUIRING_AMOUNT = frozenset(
    {
        BusinessEventName.DIRECT_ORDER_CREATED,
        BusinessEventName.DIRECT_ORDER_CONFIRMED,
        BusinessEventName.DIRECT_ORDER_DELIVERED,
        BusinessEventName.TENDER_WINNER_SELECTED,
        BusinessEventName.TENDER_ORDER_CREATED,
        BusinessEventName.TENDER_DELIVERED,
    }
)


@dataclass(frozen=True)
class BusinessEvent:
    """The row shape of the future ``analytics.business_events`` table.

    Field choices follow the mission's own sketch, adapted to what this
    repo's models actually expose (see the architecture doc):

    - ``buyer_id``/``producer_id`` are the marketplace-identity FKs
      (BuyerProfile/Producer), never `auth.users.id` directly — an actor can
      hold both roles (Phase A finding), so an event must say WHICH role
      acted, not just which user.
    - ``canonical_quantity``/``canonical_unit``/``measurement_family`` are
      computed once at emission time via ``analytics.units`` — never
      recomputed downstream from the raw ``quantity``/``unit`` (that would
      let two readers disagree on what the same row means).
    - ``idempotency_key`` follows the exact same discipline as
      ``select_winning_bid``'s / the outbox's own keys (Phase A finding):
      deterministic from the source-of-truth transition, e.g.
      ``f"{event_name}:{entity_id}"`` for a one-time transition, so a
      WhatsApp retry or a cron re-run never double-counts.
    """

    event_name: BusinessEventName
    journey: Journey
    actor_type: str  # "BUYER" | "PRODUCER" | "SYSTEM" | "ADMIN"
    actor_id: Optional[str]
    buyer_id: Optional[str]
    producer_id: Optional[str]
    entity_type: str  # "ORDER" | "AUCTION" | "BID" | "RECURRING_NEED" | "RECURRING_NEED_OCCURRENCE" | ...
    entity_id: str
    category_id: Optional[str]
    sub_category_id: Optional[str]
    zone_id: Optional[str]
    quantity: Optional[float]
    unit: Optional[str]
    canonical_quantity: Optional[float]
    canonical_unit: Optional[str]
    measurement_family: Optional[str]
    amount: Optional[float]
    currency: str
    metadata: dict = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=datetime.utcnow)
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        if not self.entity_id:
            raise ValueError(f"{self.event_name}: entity_id is required.")
        if not self.idempotency_key:
            raise ValueError(f"{self.event_name}: idempotency_key is required.")
        if self.event_name in EVENTS_REQUIRING_QUANTITY and self.quantity is None:
            raise ValueError(f"{self.event_name}: quantity is required for this event.")
        if self.event_name in EVENTS_REQUIRING_AMOUNT and self.amount is None:
            raise ValueError(f"{self.event_name}: amount is required for this event.")
        if self.journey != EVENT_JOURNEY[self.event_name]:
            raise ValueError(
                f"{self.event_name} belongs to {EVENT_JOURNEY[self.event_name]}, got {self.journey}."
            )


__all__ = [
    "BusinessEventName",
    "EVENT_JOURNEY",
    "EVENTS_REQUIRING_QUANTITY",
    "EVENTS_REQUIRING_AMOUNT",
    "BusinessEvent",
]
