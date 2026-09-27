"""Producer metric layer (Phase C): how each producer-side metric is computed from
the Producer Metric Layer daily aggregates.

Reuses the generic engine from `metric_layer.py` (`Binding`, `DataStatus`,
`MetricResult`, `TargetRow`, `compute_value`, `resolve_target`, `evaluate_target`)
rather than duplicating it — that engine has zero buyer-specific coupling. Only the
table names, dimensions and bindings below are producer-specific.

Non-negotiable rules (same as the buyer layer, restated because they bind here too)
--------------------------------------------------------------------------------
* A rate is `SUM(numerator) / SUM(denominator)` over the requested window, never an
  average of rates.
* A zero denominator gives `value = None`, never 0.
* Physical quantities are only combined inside one canonical unit.
* A metric whose source is not reliable is registered as UNAVAILABLE with the reason.
"""

from __future__ import annotations

from typing import Any

from ladini.domain.analytics.metric_layer import Binding, DataStatus

TABLE_PRODUCER = "analytics.producer_daily_metrics"
TABLE_PRODUCER_QUANTITY = "analytics.producer_quantity_daily_metrics"
TABLE_PRODUCER_SUPPLY = "analytics.producer_supply_daily_snapshot"

#: Dimensions each table can be filtered/broken down by.
TABLE_DIMENSIONS_PRODUCER: dict[str, tuple[str, ...]] = {
    TABLE_PRODUCER: ("zone_id",),
    TABLE_PRODUCER_QUANTITY: ("canonical_unit",),
    TABLE_PRODUCER_SUPPLY: ("zone_id", "category_id", "sub_category_id", "canonical_unit"),
}


def _b(**kw: Any) -> Binding:
    return Binding(**kw)


#: Standard single-table SUM/SUM bindings — `active_producers`, `available_supply`,
#: `time_to_first_sale`, `delivered_gmv_per_active_producer` and `repeat_producer_rate`
#: are NOT here: each needs a DISTINCT-count, a live-snapshot read, a point-in-time
#: duration query, or a cross-metric ratio that a single `Binding` cannot express —
#: see `ProducerAnalyticsService`'s dedicated methods for those.
BINDINGS_PRODUCER: dict[str, Binding] = {b.name: b for b in (
    _b(name="producer_order_fulfillment_rate", table=TABLE_PRODUCER,
       numerator="SUM(orders_delivered_direct + orders_delivered_tender + orders_delivered_recurring)",
       denominator="SUM(orders_confirmed_direct + orders_confirmed_tender + orders_confirmed_recurring)",
       unit="ratio", journey="GLOBAL",
       note="Delivered / confirmed producer orders, all 3 journeys combined. Confirmed = the firm "
            "commitment per journey (DIRECT: Order.status CONFIRMED; TENDER/RECURRING: the order's own "
            "creation, no separate confirmation step exists). RECURRING delivered = RECEIVED (buyer-confirmed)."),
    _b(name="producer_quantity_fulfillment_rate", table=TABLE_PRODUCER_QUANTITY,
       numerator="SUM(delivered_quantity_direct + delivered_quantity_recurring)",
       denominator="SUM(confirmed_quantity_direct + confirmed_quantity_recurring)",
       unit="ratio", journey="GLOBAL", physical=True, status=DataStatus.PARTIAL,
       note="DIRECT + RECURRING only. TENDER has no OrderItem at all (Phase A finding) so it is "
            "structurally absent here, not degraded to 0 — reliable_scope is DIRECT+RECURRING."),
    _b(name="producer_delivered_gmv", table=TABLE_PRODUCER,
       numerator="SUM(delivered_gmv_direct + delivered_gmv_tender + delivered_gmv_recurring)",
       denominator=None, unit="FCFA", journey="GLOBAL",
       note="Sum of Order.total_amount for delivered orders. Each DIRECT/TENDER/RECURRING order maps to "
            "exactly one producer (cart splitting DIRECT, winning bid TENDER, one order per producer "
            "RECURRING) so the order total is already the correct attribution — no item-level split needed."),
)}

#: Metrics deliberately NOT computed, with the exact reason — exposed by the service
#: as `status = UNAVAILABLE`, never silently absent, never proxied.
UNAVAILABLE_PRODUCER: dict[str, str] = {
    "producer_sell_through_rate": (
        "A positive PRODUCT_SELLABLE_QUANTITY_CHANGED delta does not distinguish new production from a "
        "correction, a reconciliation, a return or an adjustment — no reliable restock signal exists yet. "
        "Building a ratio off summed positive deltas would be misleading, not a lower bound."
    ),
    "producer_paid_gmv": (
        "PAID_OUT (escrow) and PAID (cash) are set atomically with DELIVERED in this codebase today "
        "(no delayed-payout path exists) — identical to producer_delivered_gmv, not a distinct signal. "
        "Not renamed/duplicated per instruction; re-open this if a delayed-payout path is ever introduced."
    ),
    "demand_exposure_rate": (
        "Mixed, not one number: DIRECT is UNAVAILABLE (no search-impression/product-visibility "
        "instrumentation), TENDER is UNAVAILABLE (no tender-notification/opportunity-seen instrumentation), "
        "RECURRING is PARTIAL (RECURRING_MATCH_FOUND is a real per-allocation signal, but not yet wired "
        "into this phase's aggregates — out of Phase C scope by mission instruction). Query "
        "RECURRING_MATCH_FOUND directly for a RECURRING-only proxy; no combined figure is proposed."
    ),
    "historical_unsold_supply": (
        "Only a live snapshot exists (producer_supply_daily_snapshot, forward from Phase C's own first "
        "run) — a PERIOD figure (newly listed minus sold, over a past window) is not reconstructible. "
        "See available_supply for the live/current gauge."
    ),
}


def reliability_of_producer(name: str) -> str:
    """Same static classification as `metric_layer.py::reliability_of`, for the producer-side
    special metrics (`ProducerAnalyticsService._SPECIAL_METRICS`) that have no `Binding` at all."""
    if name in UNAVAILABLE_PRODUCER:
        return "UNAVAILABLE"
    if name in ("active_producers", "available_supply", "delivered_gmv_per_active_producer"):
        return "RELIABLE"
    if name in ("time_to_first_sale", "repeat_producer_rate"):
        return "PARTIAL"
    binding = BINDINGS_PRODUCER.get(name)
    if binding is None:
        raise KeyError(name)
    return "PARTIAL" if binding.status == DataStatus.PARTIAL else "RELIABLE"


__all__ = [
    "TABLE_PRODUCER", "TABLE_PRODUCER_QUANTITY", "TABLE_PRODUCER_SUPPLY", "TABLE_DIMENSIONS_PRODUCER",
    "BINDINGS_PRODUCER", "UNAVAILABLE_PRODUCER", "reliability_of_producer",
]
