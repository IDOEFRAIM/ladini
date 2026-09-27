"""Pure aggregation of producer "fact rows" into the Producer Metric Layer
(Phase C) daily-metrics rows.

Same discipline as `daily_aggregation.py`, which this module reuses for unit
conversion (`resolve_canonical`, `to_canonical_quantity`, `measurement_family_of`)
rather than duplicating it: no SQL and no I/O here — `services/analytics/
producer_metrics_refresh.py` fetches one day's fact rows and hands them to
these functions; the result is exactly the rows to INSERT.

Cohort rule (same as the buyer layer): a day's row describes producer orders
CREATED that day and their CURRENT state. A late delivery therefore changes
the row of the original cohort at the next recompute.

Producer identity: every fact row's `producer_id` is resolved from the
persisted Order/OrderItem/Product/Bid relationship by the SQL that fetches
it (see `producer_metrics_refresh.py`) — never guessed here.

Numerators/denominators only — never a rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Optional

from ladini.domain.analytics.daily_aggregation import (
    NIL_UUID,
    resolve_canonical,
    to_canonical_quantity,
)
from ladini.domain.analytics.units import measurement_family_of

_ZERO = Decimal("0")

DELIVERED_DELIVERY_STATUSES = frozenset({"DELIVERED", "FULFILLED"})

#: The 3 SUPPLY-side activity signals that have no corresponding order row
#: (an order fact already implies activity on its own — see the module
#: docstring in `producer_metrics_refresh.py`).
ACTIVITY_EVENT_NAMES = frozenset({
    "PRODUCT_PUBLISHED_FOR_SALE", "PRODUCT_SELLABLE_QUANTITY_CHANGED", "TENDER_BID_RECEIVED",
})


def _id(value: Any) -> str:
    return str(value) if value else NIL_UUID


def _dec(value: Any) -> Decimal:
    if value is None:
        return _ZERO
    return value if isinstance(value, Decimal) else Decimal(str(value))


# ---------------------------------------------------------------------------
# Fact rows (what the refresher fetches)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProducerOrderFact:
    order_id: Any
    producer_id: Any
    zone_id: Any
    journey: str  # "DIRECT" | "TENDER" | "RECURRING"
    total_amount: Any
    delivery_status: str
    #: Firm commitment reached. DIRECT: Order.status CONFIRMED/COMPLETED (or the
    #: DIRECT_ORDER_CONFIRMED event, mirroring the buyer layer's own predicate).
    #: TENDER/RECURRING: always True — the order's very creation IS the
    #: commitment (select_winning_bid / accept_match_proposal), no separate
    #: confirmation step exists for either journey.
    confirmed: bool = True


@dataclass(frozen=True)
class ProducerActivityEventFact:
    event_name: str
    producer_id: Any
    zone_id: Any


@dataclass(frozen=True)
class ProducerQuantityItemFact:
    producer_id: Any
    journey: str  # "DIRECT" | "RECURRING" — TENDER never appears here (no OrderItem)
    unit: str
    priority_unit: Optional[str]
    quantity: Any
    delivery_status: str
    confirmed: bool = True


# ---------------------------------------------------------------------------
# Derived per-fact predicates
# ---------------------------------------------------------------------------


def order_is_delivered(f: "ProducerOrderFact | ProducerQuantityItemFact") -> bool:
    """DIRECT/TENDER: DELIVERED or FULFILLED. RECURRING: RECEIVED — the
    buyer-confirmed reception state, same semantics the buyer layer already
    uses for recurring (a producer's own delivery claim alone is not enough)."""
    if f.journey == "RECURRING":
        return f.delivery_status == "RECEIVED"
    return f.delivery_status in DELIVERED_DELIVERY_STATUSES


def order_is_confirmed(f: "ProducerOrderFact | ProducerQuantityItemFact") -> bool:
    """A delivered order was necessarily confirmed first (delivery requires the firm
    commitment), so delivered <= confirmed always holds — same safety net as the buyer
    layer's `direct_is_confirmed`. TENDER/RECURRING facts already default `confirmed`
    to True (their creation IS the commitment), so this only ever changes DIRECT."""
    return f.confirmed or order_is_delivered(f)


# ---------------------------------------------------------------------------
# Aggregators — one per table. Output rows are plain dicts keyed by column.
# ---------------------------------------------------------------------------

_JOURNEY_SUFFIX = {"DIRECT": "direct", "TENDER": "tender", "RECURRING": "recurring"}


def aggregate_producer_daily(
    day: date,
    orders: Iterable[ProducerOrderFact],
    activity_events: Iterable[ProducerActivityEventFact],
) -> list[dict[str, Any]]:
    """One row per (day, producer) with any qualifying activity. A producer
    with no qualifying fact that day gets no row (so COUNT(DISTINCT
    producer_id) over a window = active producers, exact — see mission
    Étape 5/6)."""
    rows: dict[str, dict[str, Any]] = {}

    def row(producer: Any, zone: Any) -> Optional[dict[str, Any]]:
        if not producer:
            return None
        key = str(producer)
        if key not in rows:
            rows[key] = {
                "metric_date": day, "producer_id": key, "zone_id": _id(zone),
                "products_published": 0, "quantity_changes": 0, "bids_received": 0,
                "orders_confirmed_direct": 0, "orders_delivered_direct": 0,
                "orders_confirmed_tender": 0, "orders_delivered_tender": 0,
                "orders_confirmed_recurring": 0, "orders_delivered_recurring": 0,
                "delivered_gmv_direct": _ZERO, "delivered_gmv_tender": _ZERO, "delivered_gmv_recurring": _ZERO,
            }
        elif rows[key]["zone_id"] == NIL_UUID and zone:
            rows[key]["zone_id"] = _id(zone)
        return rows[key]

    for o in orders:
        r = row(o.producer_id, o.zone_id)
        if r is None:
            continue
        suffix = _JOURNEY_SUFFIX[o.journey]
        if order_is_confirmed(o):
            r[f"orders_confirmed_{suffix}"] += 1
        if order_is_delivered(o):
            r[f"orders_delivered_{suffix}"] += 1
            r[f"delivered_gmv_{suffix}"] += _dec(o.total_amount)
    for e in activity_events:
        r = row(e.producer_id, e.zone_id)
        if r is None:
            continue
        if e.event_name == "PRODUCT_PUBLISHED_FOR_SALE":
            r["products_published"] += 1
        elif e.event_name == "PRODUCT_SELLABLE_QUANTITY_CHANGED":
            r["quantity_changes"] += 1
        elif e.event_name == "TENDER_BID_RECEIVED":
            r["bids_received"] += 1
    return [rows[k] for k in sorted(rows)]


def aggregate_producer_quantity_daily(day: date, items: Iterable[ProducerQuantityItemFact]) -> list[dict[str, Any]]:
    """One row per (day, producer, canonical_unit). TENDER contributes no
    facts here at all (see `ProducerQuantityItemFact`'s docstring)."""
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for it in items:
        canonical = resolve_canonical(it.unit, it.priority_unit)
        key = (_id(it.producer_id), canonical)
        if key[0] == NIL_UUID:
            continue
        r = rows.setdefault(key, {
            "metric_date": day, "producer_id": key[0], "canonical_unit": canonical,
            "measurement_family": measurement_family_of(canonical),
            "confirmed_quantity_direct": _ZERO, "delivered_quantity_direct": _ZERO,
            "confirmed_quantity_recurring": _ZERO, "delivered_quantity_recurring": _ZERO,
        })
        qty = to_canonical_quantity(it.quantity, it.unit, canonical)
        suffix = _JOURNEY_SUFFIX[it.journey]
        if order_is_confirmed(it):
            r[f"confirmed_quantity_{suffix}"] += qty
        if order_is_delivered(it):
            r[f"delivered_quantity_{suffix}"] += qty
    return [rows[k] for k in sorted(rows)]


__all__ = [
    "ProducerOrderFact", "ProducerActivityEventFact", "ProducerQuantityItemFact",
    "ACTIVITY_EVENT_NAMES", "DELIVERED_DELIVERY_STATUSES",
    "order_is_delivered", "order_is_confirmed", "aggregate_producer_daily", "aggregate_producer_quantity_daily",
]
