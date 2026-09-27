"""Metric layer: how each dictionary metric is computed from the daily aggregates.

Pure module (no SQL execution, no I/O): bindings (which table, which SUM expressions),
the explicit UNAVAILABLE registry, value/status computation, target resolution and
target status. `services/analytics/analytics_service.py` executes the queries these
bindings describe.

Non-negotiable rules
--------------------
* A rate is `SUM(numerator) / SUM(denominator)` over the requested window/grouping,
  never an average of rates (`aggregation.weighted_rate`).
* A zero denominator gives `value = None`, never 0.
* Physical quantities are only combined inside one canonical unit: when a window holds
  several units the result carries a per-unit breakdown and NO single global value.
* A metric whose source is not reliable is registered as UNAVAILABLE with the reason —
  it is never approximated from a proxy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Optional, Sequence

# SQL expressions below are module constants over whitelisted column names — never built from input.

TABLE_BUYER = "analytics.buyer_daily_metrics"
TABLE_DIRECT = "analytics.direct_daily_metrics"
TABLE_TENDER = "analytics.tender_daily_metrics"
TABLE_RECURRING = "analytics.recurring_daily_metrics"

#: Dimensions each table can be filtered/broken down by.
TABLE_DIMENSIONS: dict[str, tuple[str, ...]] = {
    TABLE_BUYER: ("zone_id",),
    TABLE_DIRECT: ("zone_id", "category_id", "sub_category_id"),
    TABLE_TENDER: ("zone_id", "category_id", "sub_category_id"),
    TABLE_RECURRING: ("zone_id", "category_id", "sub_category_id", "canonical_unit"),
}


class DataStatus(str, Enum):
    OK = "OK"
    PARTIAL = "PARTIAL"  # computed, but with a documented reliability caveat
    UNAVAILABLE = "UNAVAILABLE"  # no reliable source — value is None on purpose
    NO_DATA = "NO_DATA"  # zero denominator / no rows in the window
    MIXED_UNITS = "MIXED_UNITS"  # several incompatible units — see breakdown


class TargetStatus(str, Enum):
    ON_TARGET = "ON_TARGET"
    BELOW_TARGET = "BELOW_TARGET"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    NO_TARGET = "NO_TARGET"


@dataclass(frozen=True)
class Binding:
    name: str
    table: str
    numerator: str  # SQL aggregate expression, e.g. "SUM(matched_quantity)"
    denominator: Optional[str]  # None for plain counts/sums
    unit: str  # "count" | "ratio" | "FCFA" | "seconds" | "canonical_unit"
    journey: str
    physical: bool = False  # True => segment by canonical_unit, never one global sum
    higher_is_better: bool = True
    status: DataStatus = DataStatus.OK
    note: str = ""


def _b(**kw: Any) -> Binding:
    return Binding(**kw)


_BUYER_NEEDS = "SUM(needs_direct + needs_tender + needs_recurring)"
_BUYER_SATISFIED = "SUM(satisfied_direct + satisfied_tender + satisfied_recurring)"

BINDINGS: dict[str, Binding] = {b.name: b for b in (
    # ---- GLOBAL (buyer_daily_metrics) -------------------------------------------------------
    _b(name="needs_created", table=TABLE_BUYER, numerator=_BUYER_NEEDS, denominator=None, unit="count", journey="GLOBAL",
       note="1 order (DIRECT) + 1 auction (TENDER) + 1 non-withdrawn occurrence (RECURRING); occurrences counted on their demand date."),
    _b(name="successful_procurement_rate", table=TABLE_BUYER, numerator=_BUYER_SATISFIED, denominator=_BUYER_NEEDS,
       unit="ratio", journey="GLOBAL", status=DataStatus.PARTIAL,
       note="DIRECT+TENDER = delivered orders (reliable). RECURRING = occurrence whose orders are ALL RECEIVED (buyer-confirmed; a buyer who never confirms counts as unsatisfied, so it is a lower bound; join order_group_id has no FK). Use `reliable_scope` for DIRECT+TENDER only."),
    _b(name="potential_gmv", table=TABLE_BUYER,
       numerator="SUM(potential_gmv_direct + potential_gmv_tender + potential_gmv_recurring)", denominator=None, unit="FCFA",
       journey="GLOBAL", status=DataStatus.PARTIAL,
       note="DIRECT order totals (non-cancelled) + TENDER quantity*max price + RECURRING proposed/accepted allocation value. Three different 'potential' bases."),
    _b(name="confirmed_gmv", table=TABLE_BUYER,
       numerator="SUM(confirmed_gmv_direct + confirmed_gmv_tender + confirmed_gmv_recurring)", denominator=None,
       unit="FCFA", journey="GLOBAL",
       note="DIRECT = orders that reached CONFIRMED (producer acceptance / secured escrow); TENDER = winning-bid value; RECURRING = converted allocations."),
    _b(name="delivered_gmv", table=TABLE_BUYER,
       numerator="SUM(delivered_gmv_direct + delivered_gmv_tender + delivered_gmv_recurring)", denominator=None, unit="FCFA",
       journey="GLOBAL", status=DataStatus.PARTIAL, note="RECURRING part = orders in RECEIVED state (buyer-confirmed)."),
    # ---- DIRECT -----------------------------------------------------------------------------
    _b(name="direct_searches", table=TABLE_DIRECT, numerator="SUM(searches)", denominator=None, unit="count", journey="DIRECT",
       note="business_events only; no history before Phase C."),
    _b(name="direct_search_success_rate", table=TABLE_DIRECT, numerator="SUM(successful_searches)", denominator="SUM(searches)",
       unit="ratio", journey="DIRECT", note="Search returned >= 1 eligible result. No history before Phase C."),
    _b(name="direct_orders_per_search", table=TABLE_DIRECT, numerator="SUM(orders_created)", denominator="SUM(searches)",
       unit="orders_per_search", journey="DIRECT",
       note="Window-level ratio: orders created / searches executed. Searches and orders are not session-attributed (no search_id -> order link), so this is NOT a conversion rate and can exceed 1."),
    _b(name="direct_orders_created", table=TABLE_DIRECT, numerator="SUM(orders_created)", denominator=None, unit="count", journey="DIRECT"),
    _b(name="direct_orders_confirmed", table=TABLE_DIRECT, numerator="SUM(orders_confirmed)", denominator=None, unit="count", journey="DIRECT",
       note="Orders that reached the firm commitment (Order.status CONFIRMED)."),
    _b(name="direct_order_delivery_rate", table=TABLE_DIRECT, numerator="SUM(orders_delivered)", denominator="SUM(orders_created)",
       unit="ratio", journey="DIRECT", note="delivered / CREATED (includes orders never confirmed). See direct_fulfillment_rate for delivered / confirmed."),
    _b(name="direct_fulfillment_rate", table=TABLE_DIRECT, numerator="SUM(orders_delivered)", denominator="SUM(orders_confirmed)",
       unit="ratio", journey="DIRECT",
       note="Delivered / confirmed DIRECT orders. Confirmed = Order.status CONFIRMED (producer acceptance or secured escrow). Before the Phase D.5 event, an order confirmed then cancelled is not recoverable (slight denominator under-count). Immature cohorts read low."),
    _b(name="direct_gmv", table=TABLE_DIRECT, numerator="SUM(confirmed_value)", denominator=None, unit="FCFA", journey="DIRECT",
       note="Total of DIRECT orders that reached CONFIRMED."),
    # ---- TENDER -----------------------------------------------------------------------------
    _b(name="tenders_created", table=TABLE_TENDER, numerator="SUM(tenders_created)", denominator=None, unit="count", journey="TENDER"),
    _b(name="tender_response_rate", table=TABLE_TENDER, numerator="SUM(tenders_with_bid)", denominator="SUM(tenders_created)",
       unit="ratio", journey="TENDER"),
    _b(name="average_bids_per_tender", table=TABLE_TENDER, numerator="SUM(bids_received)", denominator="SUM(tenders_created)",
       unit="bids", journey="TENDER"),
    _b(name="time_to_first_bid", table=TABLE_TENDER, numerator="SUM(first_bid_latency_seconds_sum)",
       denominator="SUM(first_bid_latency_count)", unit="seconds", journey="TENDER", higher_is_better=False,
       note="Mean over tenders that received a bid: SUM(delay)/COUNT(tenders), never a mean of means."),
    _b(name="tender_winner_rate", table=TABLE_TENDER, numerator="SUM(tenders_with_winner)", denominator="SUM(tenders_created)",
       unit="ratio", journey="TENDER"),
    _b(name="tender_fulfillment_rate", table=TABLE_TENDER, numerator="SUM(tender_orders_delivered)",
       denominator="SUM(tenders_with_winner)", unit="ratio", journey="TENDER", status=DataStatus.PARTIAL,
       note="Delivered orders / tenders with a winner. `select_winning_bid` writes no OrderStatusHistory, so timeline history is partial."),
    _b(name="tender_gmv", table=TABLE_TENDER, numerator="SUM(committed_value)", denominator=None, unit="FCFA", journey="TENDER",
       note="quantity x winning offered price of tenders created in the window."),
    # ---- RECURRING --------------------------------------------------------------------------
    _b(name="recurring_requested_quantity", table=TABLE_RECURRING, numerator="SUM(requested_quantity)", denominator=None,
       unit="canonical_unit", journey="RECURRING", physical=True),
    _b(name="recurring_matched_quantity", table=TABLE_RECURRING, numerator="SUM(matched_quantity)", denominator=None,
       unit="canonical_unit", journey="RECURRING", physical=True),
    _b(name="recurring_confirmed_quantity", table=TABLE_RECURRING, numerator="SUM(confirmed_quantity)", denominator=None,
       unit="canonical_unit", journey="RECURRING", physical=True),
    _b(name="recurring_unmatched_quantity", table=TABLE_RECURRING, numerator="SUM(unmatched_quantity)", denominator=None,
       unit="canonical_unit", journey="RECURRING", physical=True, higher_is_better=False,
       note="UNMATCHED demand = requested - matched (matching engine outcome). It is NOT undelivered demand."),
    _b(name="recurring_coverage_rate", table=TABLE_RECURRING, numerator="SUM(matched_quantity)", denominator="SUM(requested_quantity)",
       unit="ratio", journey="RECURRING", physical=True),
    _b(name="recurring_full_coverage_rate", table=TABLE_RECURRING, numerator="SUM(occurrences_fully_covered)",
       denominator="SUM(occurrences_active)", unit="ratio", journey="RECURRING"),
    _b(name="recurring_acceptance_rate", table=TABLE_RECURRING, numerator="SUM(occurrences_accepted)",
       denominator="SUM(occurrences_notified)", unit="ratio", journey="RECURRING"),
    _b(name="recurring_skip_rate", table=TABLE_RECURRING, numerator="SUM(occurrences_skipped)",
       denominator="SUM(occurrences_total)", unit="ratio", journey="RECURRING", higher_is_better=False),
    _b(name="recurring_delivered_quantity", table=TABLE_RECURRING, numerator="SUM(delivered_quantity)", denominator=None,
       unit="canonical_unit", journey="RECURRING", physical=True, status=DataStatus.PARTIAL,
       note="Quantity of the occurrence's orders the buyer confirmed RECEIVED. RECEIVED_WITH_ISSUE is not counted (received quantity only in free text): lower bound."),
    _b(name="recurring_fulfillment_rate", table=TABLE_RECURRING, numerator="SUM(delivered_quantity)", denominator="SUM(confirmed_quantity)",
       unit="ratio", journey="RECURRING", physical=True, status=DataStatus.PARTIAL,
       note="delivered / confirmed quantity per canonical unit. Lower bound: a buyer who never confirms reception reads as undelivered; young cohorts read low."),
    _b(name="recurring_received_occurrence_rate", table=TABLE_RECURRING, numerator="SUM(occurrences_all_received)",
       denominator="SUM(occurrences_accepted)", unit="ratio", journey="RECURRING", status=DataStatus.PARTIAL,
       note="Occurrence-level receipt: ALL orders of the accepted occurrence are RECEIVED. Lower bound (needs the buyer's confirmation). NOT a quantity-based fulfillment."),
    _b(name="recurring_gmv", table=TABLE_RECURRING, numerator="SUM(confirmed_value)", denominator=None, unit="FCFA", journey="RECURRING",
       note="Value of CONVERTED (accepted) allocations."),
)}

#: Metrics deliberately NOT computed, with the exact reason. Exposed by the service as
#: `status = UNAVAILABLE` (never silently absent, never proxied).
UNAVAILABLE: dict[str, str] = {
    "fulfillment_rate": "Not aggregated across journeys yet: the buyer-level table has no per-journey 'confirmed' counts. Use direct_fulfillment_rate, tender_fulfillment_rate and recurring_fulfillment_rate.",
    "recurring_modification_rate": "RECURRING_DIGEST_MODIFIED is not instrumented.",
    "active_recurring_needs": "A point-in-time gauge whose history is not stored; the daily `needs_with_occurrence` column is not additive across days.",
}


# ---------------------------------------------------------------------------
# Value computation
# ---------------------------------------------------------------------------


def compute_value(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    """`numerator / denominator`, `None` on a zero/absent denominator (never an invented 0)."""
    if denominator is None:
        return numerator
    if denominator == 0:
        return None
    return float(numerator or 0.0) / float(denominator)


@dataclass(frozen=True)
class TargetRow:
    scope_type: str
    scope_id: Optional[str]
    target_value: float
    warning_threshold: Optional[float]
    critical_threshold: Optional[float]
    valid_from: date
    valid_until: Optional[date]


#: Most specific scope wins. Product dimension first (a sub-category target is the most specific
#: statement about what "good" means), then category, then geography, then the journey, then global.
#: A scope only applies when the query is actually filtered on it (a ZONE target never applies to
#: an all-zones query).
TARGET_PRECEDENCE = ("SUBCATEGORY", "CATEGORY", "ZONE", "JOURNEY", "GLOBAL")


def resolve_target(
    candidates: Sequence[TargetRow],
    *,
    on: date,
    zone_id: Optional[str] = None,
    category_id: Optional[str] = None,
    sub_category_id: Optional[str] = None,
    journey: Optional[str] = None,
) -> Optional[TargetRow]:
    applicable: dict[str, list[TargetRow]] = {}
    for t in candidates:
        if t.valid_from > on or (t.valid_until is not None and t.valid_until < on):
            continue
        matches = (
            t.scope_type == "GLOBAL"
            or (t.scope_type == "SUBCATEGORY" and sub_category_id is not None and t.scope_id == str(sub_category_id))
            or (t.scope_type == "CATEGORY" and category_id is not None and t.scope_id == str(category_id))
            or (t.scope_type == "ZONE" and zone_id is not None and t.scope_id == str(zone_id))
            or (t.scope_type == "JOURNEY" and journey is not None and t.scope_id == journey)
        )
        if matches:
            applicable.setdefault(t.scope_type, []).append(t)
    for scope in TARGET_PRECEDENCE:
        if scope in applicable:
            # Same scope, several rows: the most recently effective one.
            return max(applicable[scope], key=lambda t: t.valid_from)
    return None


def evaluate_target(value: Optional[float], target: Optional[TargetRow], *, higher_is_better: bool = True) -> TargetStatus:
    """warning/critical are the two 'worse than target' bands (target > warning > critical for
    higher-is-better metrics; mirrored for lower-is-better)."""
    if target is None:
        return TargetStatus.NO_TARGET
    if value is None:
        return TargetStatus.NO_TARGET
    sign = 1.0 if higher_is_better else -1.0
    v = sign * value
    if v >= sign * target.target_value:
        return TargetStatus.ON_TARGET
    if target.critical_threshold is not None and v < sign * target.critical_threshold:
        return TargetStatus.CRITICAL
    if target.warning_threshold is not None and v < sign * target.warning_threshold:
        return TargetStatus.WARNING
    return TargetStatus.BELOW_TARGET


@dataclass
class MetricResult:
    """Common response contract of the metric layer."""

    metric_name: str
    value: Optional[float]
    numerator: Optional[float]
    denominator: Optional[float]
    unit: Optional[str]
    status: DataStatus
    period: dict[str, str]
    target: Optional[dict[str, Any]] = None
    target_status: TargetStatus = TargetStatus.NO_TARGET
    previous_value: Optional[float] = None
    delta: Optional[float] = None
    breakdown: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["status"] = self.status.value
        d["target_status"] = self.target_status.value
        return d


def unavailable_result(name: str, period: dict[str, str]) -> MetricResult:
    return MetricResult(metric_name=name, value=None, numerator=None, denominator=None, unit=None,
                        status=DataStatus.UNAVAILABLE, period=period, notes=[UNAVAILABLE[name]])


__all__ = [
    "Binding", "BINDINGS", "UNAVAILABLE", "DataStatus", "TargetStatus", "TargetRow", "MetricResult",
    "TABLE_BUYER", "TABLE_DIRECT", "TABLE_TENDER", "TABLE_RECURRING", "TABLE_DIMENSIONS",
    "TARGET_PRECEDENCE", "compute_value", "resolve_target", "evaluate_target", "unavailable_result",
]
