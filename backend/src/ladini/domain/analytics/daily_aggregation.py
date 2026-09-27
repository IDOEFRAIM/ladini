"""Pure aggregation of buyer "fact rows" into the Phase D daily-metrics rows.

No SQL and no I/O here: `services/analytics/daily_metrics_refresh.py` fetches
one day's fact rows (light joins over the transactional tables and
`business_events`) and hands them to these functions; the result is exactly
the rows to INSERT. Keeping the logic pure makes every counting rule
unit-testable without PostgreSQL and guarantees a replay of the same facts
yields byte-identical rows (deterministic output ordering included).

Cohort rule (see docs/analytics/METRIC_LAYER.md): a day's row describes the
needs CREATED that day (orders/auctions by creation date, recurring
occurrences by their demand date) and their CURRENT state. A late delivery
therefore changes the row of the original cohort at the next recompute.

Numerators/denominators only — never a rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Optional

from ladini.domain.analytics.units import convert_to_canonical, measurement_family_of

#: "Not attributable / unknown" dimension value. Stored instead of NULL so the
#: UNIQUE grain index really enforces one row per grain.
NIL_UUID = "00000000-0000-0000-0000-000000000000"

_ZERO = Decimal("0")

#: Order/auction rows that are not real needs (checkout drafts).
NON_NEED_ORDER_STATUSES = frozenset({"DRAFT", "SUPERSEDED"})
DELIVERED_DELIVERY_STATUSES = frozenset({"DELIVERED", "FULFILLED"})
#: Occurrence statuses that mean the buyer withdrew the demand.
WITHDRAWN_OCCURRENCE_STATUSES = frozenset({"SKIPPED", "CANCELLED"})
ACCEPTED_OCCURRENCE_STATUSES = frozenset({"ACCEPTED", "PARTIALLY_ACCEPTED"})


def _id(value: Any) -> str:
    return str(value) if value else NIL_UUID


def _dec(value: Any) -> Decimal:
    if value is None:
        return _ZERO
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _q3(value: float) -> Decimal:
    return Decimal(str(round(float(value), 3)))


# ---------------------------------------------------------------------------
# Fact rows (what the refresher fetches)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DirectOrderFact:
    buyer_id: Any
    zone_id: Any
    category_id: Any
    sub_category_id: Any
    total_amount: Any
    status: str
    delivery_status: str
    #: Firm commitment reached (SQL: business event DIRECT_ORDER_CONFIRMED, or Order.status
    #: CONFIRMED/COMPLETED). Delivered orders are always counted confirmed (see direct_is_confirmed).
    confirmed: bool = False


@dataclass(frozen=True)
class SearchFact:
    zone_id: Any
    succeeded: bool  # True = a DIRECT_SEARCH_SUCCEEDED event, False = DIRECT_SEARCH_PERFORMED


@dataclass(frozen=True)
class TenderFact:
    buyer_id: Any
    zone_id: Any
    category_id: Any
    sub_category_id: Any
    quantity: Any
    max_price_per_unit: Any
    bids_count: int
    first_bid_latency_seconds: Optional[float]
    has_winner: bool
    winner_price: Any
    orders_count: int
    delivered_orders_count: int


@dataclass(frozen=True)
class RecurringFact:
    occurrence_id: Any
    need_id: Any
    buyer_id: Any
    zone_id: Any
    category_id: Any
    sub_category_id: Any
    priority_unit: Optional[str]
    unit: str
    requested_quantity: Any
    quantity_matched: Any
    quantity_confirmed: Any
    status: str
    notified: bool
    potential_value: Any
    confirmed_value: Any
    orders_count: int
    received_orders_count: int
    received_value: Any
    #: SUM of the items of this occurrence's CONVERTED allocations whose order is RECEIVED
    #: (source of truth; the `quantity_delivered` column is written from the same rule).
    delivered_quantity: Any = 0


@dataclass(frozen=True)
class DigestFact:
    buyer_id: Any
    zone_id: Any
    accepted: bool  # False = RECURRING_DIGEST_SENT (queued), True = RECURRING_DIGEST_ACCEPTED


# ---------------------------------------------------------------------------
# Derived per-fact predicates (the business rules, each named once)
# ---------------------------------------------------------------------------


def direct_is_need(f: DirectOrderFact) -> bool:
    return f.status not in NON_NEED_ORDER_STATUSES


def direct_is_delivered(f: DirectOrderFact) -> bool:
    """DIRECT/TENDER lifecycle: DELIVERED or FULFILLED (see the Phase C delivery rule)."""
    return f.delivery_status in DELIVERED_DELIVERY_STATUSES


def direct_is_confirmed(f: DirectOrderFact) -> bool:
    """Confirmed = the firm commitment was reached. A delivered order was necessarily confirmed
    (delivery requires Order.status CONFIRMED), so delivered <= confirmed always holds."""
    return f.confirmed or direct_is_delivered(f)


def direct_potential_value(f: DirectOrderFact) -> Decimal:
    return _ZERO if f.status == "CANCELLED" else _dec(f.total_amount)


def tender_is_delivered(f: TenderFact) -> bool:
    return f.delivered_orders_count > 0


def recurring_is_active(f: RecurringFact) -> bool:
    return f.status not in WITHDRAWN_OCCURRENCE_STATUSES


def recurring_is_fully_covered(f: RecurringFact) -> bool:
    return recurring_is_active(f) and _dec(f.requested_quantity) > 0 and _dec(f.quantity_matched) >= _dec(f.requested_quantity)


def recurring_is_all_received(f: RecurringFact) -> bool:
    """Occurrence-level RECEIVED: it has orders (via order_group_id == orders.checkout_group_id,
    created in the same transaction as the acceptance) and EVERY one of them reached RECEIVED.
    `RECEIVED_WITH_ISSUE`, cancelled or still-pending orders keep it out (never a success)."""
    return f.orders_count > 0 and f.received_orders_count == f.orders_count


def resolve_canonical(unit: str, priority_unit: Optional[str]) -> str:
    """Canonical unit of a recurring occurrence: the sub-category `priority_unit` when the
    occurrence unit converts to it (same MASS/VOLUME family), else the occurrence's own unit.
    Never a guessed conversion (SAC stays SAC, TETE stays TETE)."""
    unit = (unit or "").upper().strip()
    if priority_unit:
        target = priority_unit.upper().strip()
        if target == unit or convert_to_canonical(1.0, unit, target) is not None:
            return target
    return unit


def to_canonical_quantity(quantity: Any, unit: str, canonical_unit: str) -> Decimal:
    q = _dec(quantity)
    unit = (unit or "").upper().strip()
    if unit == canonical_unit:
        return q
    converted = convert_to_canonical(float(q), unit, canonical_unit)
    # `resolve_canonical` only returns a target the unit converts to, so None cannot happen here.
    return _q3(converted if converted is not None else float(q))


# ---------------------------------------------------------------------------
# Aggregators — one per table. Output rows are plain dicts keyed by column.
# ---------------------------------------------------------------------------


def aggregate_direct(day: date, orders: Iterable[DirectOrderFact], searches: Iterable[SearchFact]) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}

    def row(zone: Any, cat: Any, sub: Any) -> dict[str, Any]:
        key = (_id(zone), _id(cat), _id(sub))
        if key not in rows:
            rows[key] = {
                "metric_date": day, "zone_id": key[0], "category_id": key[1], "sub_category_id": key[2],
                "searches": 0, "successful_searches": 0, "orders_created": 0, "orders_confirmed": 0,
                "orders_delivered": 0, "created_value": _ZERO, "confirmed_value": _ZERO, "delivered_value": _ZERO,
            }
        return rows[key]

    for s in searches:
        r = row(s.zone_id, None, None)
        if s.succeeded:
            r["successful_searches"] += 1
        else:
            r["searches"] += 1
    for o in orders:
        if not direct_is_need(o):
            continue
        r = row(o.zone_id, o.category_id, o.sub_category_id)
        r["orders_created"] += 1
        r["created_value"] += direct_potential_value(o)
        if direct_is_confirmed(o):
            r["orders_confirmed"] += 1
            r["confirmed_value"] += _dec(o.total_amount)
        if direct_is_delivered(o):
            r["orders_delivered"] += 1
            r["delivered_value"] += _dec(o.total_amount)
    return [rows[k] for k in sorted(rows)]


def aggregate_tender(day: date, tenders: Iterable[TenderFact]) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for t in tenders:
        key = (_id(t.zone_id), _id(t.category_id), _id(t.sub_category_id))
        r = rows.setdefault(key, {
            "metric_date": day, "zone_id": key[0], "category_id": key[1], "sub_category_id": key[2],
            "tenders_created": 0, "tenders_with_bid": 0, "bids_received": 0, "tenders_with_winner": 0,
            "tender_orders_created": 0, "tender_orders_delivered": 0,
            "first_bid_latency_seconds_sum": _ZERO, "first_bid_latency_count": 0,
            "potential_value": _ZERO, "committed_value": _ZERO, "delivered_value": _ZERO,
        })
        qty = _dec(t.quantity)
        r["tenders_created"] += 1
        r["bids_received"] += t.bids_count
        r["potential_value"] += qty * _dec(t.max_price_per_unit)
        if t.bids_count > 0:
            r["tenders_with_bid"] += 1
            if t.first_bid_latency_seconds is not None:
                r["first_bid_latency_seconds_sum"] += _dec(max(t.first_bid_latency_seconds, 0.0))
                r["first_bid_latency_count"] += 1
        if t.has_winner:
            r["tenders_with_winner"] += 1
            r["committed_value"] += qty * _dec(t.winner_price)
            if tender_is_delivered(t):
                r["delivered_value"] += qty * _dec(t.winner_price)
        r["tender_orders_created"] += t.orders_count
        r["tender_orders_delivered"] += t.delivered_orders_count
    return [rows[k] for k in sorted(rows)]


def aggregate_recurring(day: date, facts: Iterable[RecurringFact]) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    needs_seen: dict[tuple[str, str, str, str], set[str]] = {}
    for f in facts:
        canonical = resolve_canonical(f.unit, f.priority_unit)
        key = (_id(f.zone_id), _id(f.category_id), _id(f.sub_category_id), canonical)
        r = rows.setdefault(key, {
            "metric_date": day, "zone_id": key[0], "category_id": key[1], "sub_category_id": key[2],
            "canonical_unit": canonical, "measurement_family": measurement_family_of(canonical),
            "occurrences_total": 0, "occurrences_active": 0, "occurrences_fully_covered": 0,
            "occurrences_notified": 0, "occurrences_accepted": 0, "occurrences_skipped": 0,
            "occurrences_with_orders": 0, "occurrences_all_received": 0, "needs_with_occurrence": 0,
            "requested_quantity": _ZERO, "matched_quantity": _ZERO, "confirmed_quantity": _ZERO,
            "delivered_quantity": _ZERO, "unmatched_quantity": _ZERO, "potential_value": _ZERO, "confirmed_value": _ZERO, "received_value": _ZERO,
        })
        needs_seen.setdefault(key, set()).add(str(f.need_id))
        r["occurrences_total"] += 1
        if f.status == "SKIPPED":
            r["occurrences_skipped"] += 1
        if f.notified:
            r["occurrences_notified"] += 1
        if f.status in ACCEPTED_OCCURRENCE_STATUSES:
            r["occurrences_accepted"] += 1
        if f.orders_count > 0:
            r["occurrences_with_orders"] += 1
        if recurring_is_all_received(f):
            r["occurrences_all_received"] += 1
        r["received_value"] += _dec(f.received_value)
        if not recurring_is_active(f):
            continue
        requested = to_canonical_quantity(f.requested_quantity, f.unit, canonical)
        matched = to_canonical_quantity(f.quantity_matched, f.unit, canonical)
        confirmed = to_canonical_quantity(f.quantity_confirmed, f.unit, canonical)
        r["occurrences_active"] += 1
        if recurring_is_fully_covered(f):
            r["occurrences_fully_covered"] += 1
        r["requested_quantity"] += requested
        r["matched_quantity"] += matched
        r["confirmed_quantity"] += confirmed
        r["delivered_quantity"] += to_canonical_quantity(f.delivered_quantity, f.unit, canonical)
        r["unmatched_quantity"] += max(requested - matched, _ZERO)
        r["potential_value"] += _dec(f.potential_value)
        r["confirmed_value"] += _dec(f.confirmed_value)
    for key, r in rows.items():
        r["needs_with_occurrence"] = len(needs_seen[key])
    return [rows[k] for k in sorted(rows)]


def aggregate_buyer(
    day: date,
    direct: Iterable[DirectOrderFact],
    tenders: Iterable[TenderFact],
    recurring: Iterable[RecurringFact],
    digests: Iterable[DigestFact],
) -> list[dict[str, Any]]:
    """One row per (day, buyer) with any need or digest activity. A buyer with no need instance
    that day gets no row (so COUNT(DISTINCT buyer_id) over a window = active buyers)."""
    rows: dict[str, dict[str, Any]] = {}

    def row(buyer: Any, zone: Any) -> Optional[dict[str, Any]]:
        if not buyer:
            return None  # walk-in / unattributed order: not a buyer-analytics fact
        key = str(buyer)
        if key not in rows:
            rows[key] = {
                "metric_date": day, "buyer_id": key, "zone_id": _id(zone),
                "needs_direct": 0, "needs_tender": 0, "needs_recurring": 0,
                "satisfied_direct": 0, "satisfied_tender": 0, "satisfied_recurring": 0,
                "potential_gmv_direct": _ZERO, "potential_gmv_tender": _ZERO, "potential_gmv_recurring": _ZERO,
                "confirmed_gmv_direct": _ZERO, "confirmed_gmv_tender": _ZERO, "confirmed_gmv_recurring": _ZERO,
                "delivered_gmv_direct": _ZERO, "delivered_gmv_tender": _ZERO, "delivered_gmv_recurring": _ZERO,
                "digests_queued": 0, "digests_accepted": 0,
            }
        elif rows[key]["zone_id"] == NIL_UUID and zone:
            rows[key]["zone_id"] = _id(zone)
        return rows[key]

    for o in direct:
        if not direct_is_need(o):
            continue
        r = row(o.buyer_id, o.zone_id)
        if r is None:
            continue
        r["needs_direct"] += 1
        r["potential_gmv_direct"] += direct_potential_value(o)
        if direct_is_confirmed(o):
            r["confirmed_gmv_direct"] += _dec(o.total_amount)
        if direct_is_delivered(o):
            r["satisfied_direct"] += 1
            r["delivered_gmv_direct"] += _dec(o.total_amount)
    for t in tenders:
        r = row(t.buyer_id, t.zone_id)
        if r is None:
            continue
        qty = _dec(t.quantity)
        r["needs_tender"] += 1
        r["potential_gmv_tender"] += qty * _dec(t.max_price_per_unit)
        if t.has_winner:
            r["confirmed_gmv_tender"] += qty * _dec(t.winner_price)
        if tender_is_delivered(t):
            r["satisfied_tender"] += 1
            r["delivered_gmv_tender"] += qty * _dec(t.winner_price)
    for f in recurring:
        if not recurring_is_active(f):
            continue
        r = row(f.buyer_id, f.zone_id)
        if r is None:
            continue
        r["needs_recurring"] += 1
        r["potential_gmv_recurring"] += _dec(f.potential_value)
        r["confirmed_gmv_recurring"] += _dec(f.confirmed_value)
        if recurring_is_all_received(f):
            r["satisfied_recurring"] += 1
        r["delivered_gmv_recurring"] += _dec(f.received_value)
    for d in digests:
        r = row(d.buyer_id, d.zone_id)
        if r is None:
            continue
        r["digests_accepted" if d.accepted else "digests_queued"] += 1
    return [rows[k] for k in sorted(rows)]


__all__ = [
    "NIL_UUID", "DirectOrderFact", "SearchFact", "TenderFact", "RecurringFact", "DigestFact",
    "aggregate_direct", "aggregate_tender", "aggregate_recurring", "aggregate_buyer",
    "resolve_canonical", "to_canonical_quantity", "direct_is_delivered", "recurring_is_all_received",
]
