"""Buyer metric dictionary — THE single source of truth for every canonical
buyer-journey KPI's definition.

Nothing downstream (a future `AnalyticsService`, an admin endpoint, a
frontend chart) is meant to hand-roll its own numerator/denominator: it
looks up a `MetricDefinition` here and applies it. Adding a metric means
adding one entry here, never a copy-pasted SQL query with its own private
idea of what "coverage" means.

Design rules this registry enforces (see the architecture doc for the full
rationale):

- a ratio's numerator and denominator are always SUMMED separately before
  dividing (`aggregation.weighted_rate`) — never an average of child rates.
- `unit_behavior` is explicit for every metric that touches a physical
  quantity — `ONLY_COMPATIBLE_CANONICAL_UNITS` means "this metric's
  numerator/denominator must be aggregated via
  `analytics.units.units_are_aggregation_compatible`", not summed blindly.
- `reconstructible_historically` is never "YES" by assumption — every entry
  cites the columns/timestamps that make it true, or says PARTIAL/NO and
  says why (mission: "ne fabrique aucun historique manquant").
- no metric definition ever branches on a hardcoded product/category name;
  every journey-scoped metric is expressed over `journey`/`category_id`/
  `sub_category_id` as dimensions, never `if product == "tomate"`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Journey(str, Enum):
    GLOBAL = "GLOBAL"
    DIRECT = "DIRECT"
    TENDER = "TENDER"
    RECURRING = "RECURRING"
    #: Producer Analytics Phase B — supply-side facts (a quantity/visibility
    #: change on a Product) are not tied to any one buyer journey: the same
    #: product can be sold via DIRECT, TENDER or RECURRING. Not a 4th buyer
    #: funnel, a separate axis.
    SUPPLY = "SUPPLY"


class AggregationType(str, Enum):
    #: A plain count of rows/distinct entities in the window.
    COUNT = "COUNT"
    #: A sum of a physical quantity or a monetary amount.
    SUM = "SUM"
    #: numerator/denominator both summed across the requested grouping, then
    #: divided ONCE — see `aggregation.weighted_rate`. Covers both
    #: "quantity ratios" (coverage) and "count ratios" (response rate).
    WEIGHTED_RATIO = "WEIGHTED_RATIO"
    #: Sum of a duration divided by a count — same weighted-average
    #: discipline as WEIGHTED_RATIO, kept as its own label because the unit
    #: of the result is time, not a dimensionless fraction.
    DURATION_AVG = "DURATION_AVG"
    #: A point-in-time snapshot count (e.g. "how many ACTIVE recurring needs
    #: right now"), not a rate over a window.
    GAUGE = "GAUGE"


class Reconstructibility(str, Enum):
    #: Computable for any past window from data already durably stored.
    YES = "YES"
    #: Computable only from some point onward, or only for a sub-population.
    PARTIAL = "PARTIAL"
    #: Requires instrumentation that does not exist yet (Phase C) — zero
    #: historical values may be fabricated for it.
    NO = "NO"


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    description: str
    business_definition: str
    journey: Journey
    aggregation_type: AggregationType
    numerator: Optional[str]
    denominator: Optional[str]
    unit_behavior: str
    supported_dimensions: tuple[str, ...]
    supported_time_windows: tuple[str, ...]
    source_entities: tuple[str, ...]
    reconstructible_historically: Reconstructibility
    reconstructible_note: str
    #: Set when this name is a pure filtered view of another metric (e.g.
    #: `direct_gmv` == `confirmed_gmv` WHERE journey=DIRECT) — the registry
    #: keeps it as its own catalogue entry (mission listed it by this name)
    #: but computation must delegate to `alias_of`, never re-derive.
    alias_of: Optional[str] = None


_STANDARD_DIMENSIONS = ("date", "zone", "category", "sub_category", "journey", "buyer")
_STANDARD_WINDOWS = ("DAY", "WEEK", "MONTH", "CUSTOM_RANGE")

# A "need instance" is the one thing every metric below agrees on so that
# `needs_created` and `successful_procurement_rate` mean the same kind of
# object in all three journeys (mission section 11 — "needs_created" must be
# homogeneous or the ambiguity must be resolved, not hidden):
#   DIRECT   -> one Order (order_type IN ('STANDARD','DIRECT_SALE'); PREORDER
#               excluded — it belongs to the separate future-production
#               preorder subsystem, not yet mapped to a journey, see the doc)
#   TENDER   -> one Auction
#   RECURRING-> one RecurringNeedOccurrence (NOT the parent RecurringNeed,
#               which is a standing template/subscription, not itself an
#               instance of expressed demand — see `active_recurring_needs`
#               below for the template-level gauge instead)
# "Satisfied" (the North Star's numerator) means the instance's terminal
# state is a REAL delivery, not merely a match/confirmation. Phase C traced
# every writer of `Order.delivery_status` (not just the two literal names)
# and found DELIVERED and FULFILLED are NOT synonyms — they belong to
# different, non-overlapping flows:
#   - FULFILLED is written ONCE, at creation, only by `record_sale`
#     (order_type=DIRECT_SALE, an agent-recorded walk-in sale — the buyer is
#     physically present at the exchange, so there is no separate delivery
#     step to track).
#   - DELIVERED is written by `EscrowMixin.verify_delivery_otp` (buyer
#     supplies the OTP — buyer-attested), `ProducerMgmtMixin.
#     confirm_delivery_and_payment` (cash-on-delivery: producer declares
#     delivery+payment in one gesture — accepted as final for that business
#     model, order_type STANDARD/PREORDER or tender-linked), AND, for
#     order_type=RECURRING_SUPPLY specifically, by
#     `RecurringSupplyMixin.mark_order_delivery_status` as an INTERMEDIATE
#     producer-declared step (PENDING -> IN_TRANSIT -> DELIVERED) that is
#     NOT yet buyer-confirmed for that flow: `record_order_reception` then
#     moves it to RECEIVED ("tout est bon", the real buyer confirmation) or
#     RECEIVED_WITH_ISSUE (an unresolved problem — never counted as success).
# Net rule (order_type-conditional, not a single flat literal set):
#   DIRECT/TENDER (order_type != RECURRING_SUPPLY)
#       -> Order.delivery_status IN ('DELIVERED', 'FULFILLED')
#   RECURRING_SUPPLY orders (which back RECURRING occurrences)
#       -> Order.delivery_status == 'RECEIVED' only — 'DELIVERED' alone is
#          insufficient (still awaiting buyer confirmation), and
#          'RECEIVED_WITH_ISSUE' is explicitly excluded.
# REAL BUG FOUND while tracing this (not fixed here — a schema/instrumentation
# gap, not a unit-model one): `RecurringNeedOccurrence.quantity_delivered`
# has ZERO writers anywhere in the codebase — declared on the model, never
# assigned. Phase B's dictionary wrongly assumed it was historically
# populated; corrected below (`recurring_delivered_quantity`,
# `delivered_gmv`'s RECURRING contribution, `recurring_fulfillment_rate` are
# now Reconstructibility.NO for that reason, not YES). The only real signal
# for "was this occurrence's supply received" is its RECURRING_SUPPLY
# Order's delivery_status, joined via `occurrence.order_group_id` — nullable,
# no FK (Phase A finding) — hence PARTIAL, not YES, for the North Star's
# RECURRING contribution.

METRICS: dict[str, MetricDefinition] = {}


def _register(defn: MetricDefinition) -> MetricDefinition:
    if defn.name in METRICS:
        raise ValueError(f"Duplicate metric name in dictionary: {defn.name!r}")
    METRICS[defn.name] = defn
    return defn


# ---------------------------------------------------------------------------
# GLOBAL
# ---------------------------------------------------------------------------

active_buyers = _register(
    MetricDefinition(
        name="active_buyers",
        description="Distinct buyers with at least one qualifying action in the window.",
        business_definition=(
            "COUNT(DISTINCT buyer) across BuyerProfile-linked users who created at least one "
            "need instance (an Order, an Auction, or a RecurringNeedOccurrence) in the window. "
            "Does NOT count a raw DIRECT search as activity (not reconstructible historically, "
            "and a search alone is too weak a signal of intent) — see `direct_searches` for that "
            "separate, weaker signal."
        ),
        journey=Journey.GLOBAL,
        aggregation_type=AggregationType.COUNT,
        numerator="COUNT(DISTINCT buyer_id)",
        denominator=None,
        unit_behavior="DIMENSIONLESS_COUNT",
        supported_dimensions=("date", "zone"),
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.orders", "marketplace.auctions", "marketplace.recurring_need_occurrences"),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="Order/Auction/RecurringNeedOccurrence all carry created_at + buyer linkage historically.",
    )
)

needs_created = _register(
    MetricDefinition(
        name="needs_created",
        description="Count of buyer need instances created in the window, across all 3 journeys.",
        business_definition=(
            "COUNT of need instances (see module docstring for the exact per-journey definition: "
            "1 Order for DIRECT, 1 Auction for TENDER, 1 RecurringNeedOccurrence for RECURRING). "
            "This is the North Star's denominator population."
        ),
        journey=Journey.GLOBAL,
        aggregation_type=AggregationType.COUNT,
        numerator="COUNT(need_instance)",
        denominator=None,
        unit_behavior="DIMENSIONLESS_COUNT",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.orders", "marketplace.auctions", "marketplace.recurring_need_occurrences"),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="All three source tables' created_at are historically populated.",
    )
)

successful_procurement_rate = _register(
    MetricDefinition(
        name="successful_procurement_rate",
        description="NORTH STAR — share of expressed buyer needs that were actually delivered.",
        business_definition=(
            "WeightedRate(satisfied need instances, needs_created) — see module docstring for "
            "the exact per-journey 'satisfied' (=delivered) definition. Deliberately stricter than "
            "'matched': a RECURRING occurrence that matched but never reached full delivery does "
            "NOT count, by design (mission: \"'satisfait' doit être lié à un état réellement "
            "délivré, pas juste matché\")."
        ),
        journey=Journey.GLOBAL,
        aggregation_type=AggregationType.WEIGHTED_RATIO,
        numerator="SUM(satisfied need instances)",
        denominator="SUM(needs_created)",
        unit_behavior="DIMENSIONLESS_RATIO",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.orders", "marketplace.auctions", "marketplace.recurring_need_occurrences"),
        reconstructible_historically=Reconstructibility.PARTIAL,
        reconstructible_note=(
            "YES for DIRECT/TENDER (DELIVERED-vs-FULFILLED resolved in Phase C — see module "
            "docstring: order_type-conditional, both are historically populated terminal states). "
            "PARTIAL for RECURRING only: the real 'received' signal is the linked RECURRING_SUPPLY "
            "Order's delivery_status via occurrence.order_group_id, which has no FK — a real but "
            "imperfect historical join, not a hard guarantee."
        ),
    )
)

fulfillment_rate = _register(
    MetricDefinition(
        name="fulfillment_rate",
        description="Execution quality: of need instances that reached a firm commitment, how many were actually delivered.",
        business_definition=(
            "WeightedRate(delivered need instances, confirmed need instances) — a NARROWER funnel "
            "stage than successful_procurement_rate (confirmed -> delivered, not expressed -> "
            "delivered). Blends direct_fulfillment_rate / tender_fulfillment_rate / "
            "recurring_fulfillment_rate the same way successful_procurement_rate blends the "
            "wider funnel."
        ),
        journey=Journey.GLOBAL,
        aggregation_type=AggregationType.WEIGHTED_RATIO,
        numerator="SUM(delivered need instances)",
        denominator="SUM(confirmed need instances)",
        unit_behavior="DIMENSIONLESS_RATIO",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.orders", "marketplace.auctions", "marketplace.recurring_need_occurrences"),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="Same source columns as successful_procurement_rate, narrower population.",
    )
)

repeat_buyer_rate = _register(
    MetricDefinition(
        name="repeat_buyer_rate",
        description="Share of buyers with a successful need who had a SECOND successful need in the window.",
        business_definition=(
            "WeightedRate(buyers with >=2 satisfied need instances, buyers with >=1 satisfied need "
            "instance), over a rolling 90-day window (PROVISIONAL default — the mission asked to "
            "audit real usage data before fixing the window; no live data access was available in "
            "this pass, so 90 days is proposed, not fixed. Revisit with real distributions before "
            "Phase C dashboards ship). 'Successful' reuses the exact same satisfied-instance "
            "definition as the North Star, deliberately (a repeat of a FAILED need should not "
            "count as loyalty)."
        ),
        journey=Journey.GLOBAL,
        aggregation_type=AggregationType.WEIGHTED_RATIO,
        numerator="COUNT(DISTINCT buyer_id WHERE satisfied_instance_count >= 2)",
        denominator="COUNT(DISTINCT buyer_id WHERE satisfied_instance_count >= 1)",
        unit_behavior="DIMENSIONLESS_RATIO",
        supported_dimensions=("date", "zone"),
        supported_time_windows=("ROLLING_90D", "CUSTOM_RANGE"),
        source_entities=("marketplace.orders", "marketplace.auctions", "marketplace.recurring_need_occurrences"),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="Reuses successful_procurement_rate's already-reconstructible signal.",
    )
)

potential_gmv = _register(
    MetricDefinition(
        name="potential_gmv",
        description="Value of need instances AT CREATION TIME, before any confirmation — the buyer's stated ask.",
        business_definition=(
            "SUM of (quantity x quoted/ceiling price) at the moment the need instance was created, "
            "regardless of outcome. DIRECT: SUM(order_items.price_at_sale * order_items.quantity) "
            "at order creation. TENDER: SUM(auction.quantity * auction.max_price_per_unit) — the "
            "buyer's own stated ceiling, a real number, not a guess. RECURRING: SUM(occurrence."
            "requested_quantity * reference_price), where reference_price falls back, in order, to "
            "governance.standard_prices (sub_category+zone) then RecurringNeed.max_price_per_unit; "
            "an occurrence with neither is excluded (marked NULL, never assumed KG@0)."
        ),
        journey=Journey.GLOBAL,
        aggregation_type=AggregationType.SUM,
        numerator="SUM(quantity * price_at_creation)",
        denominator=None,
        unit_behavior="CURRENCY_FCFA",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=(
            "marketplace.order_items", "marketplace.auctions",
            "marketplace.recurring_need_occurrences", "governance.standard_prices",
        ),
        reconstructible_historically=Reconstructibility.PARTIAL,
        reconstructible_note=(
            "YES for DIRECT/TENDER (both a stored price exist at creation). PARTIAL for RECURRING: "
            "only reconstructible for occurrences whose sub_category+zone had a standard_prices row "
            "or whose need had max_price_per_unit set at the time — neither is guaranteed historically."
        ),
    )
)

confirmed_gmv = _register(
    MetricDefinition(
        name="confirmed_gmv",
        description="Value of need instances that reached a REAL, priced commitment (not yet necessarily delivered).",
        business_definition=(
            "DIRECT: SUM(order_items) for orders with status='CONFIRMED' or payment_status IN "
            "('PAID','ESCROWED'), at price_at_sale (the real transacted price, not potential). "
            "TENDER: SUM(auction.quantity * winning_bid.offered_price) for auctions with a selected "
            "winner. RECURRING: SUM(need_allocations.quantity * need_allocations.unit_price) for "
            "allocations with status='ACCEPTED' (i.e. actually converted via accept_match_proposal, "
            "not merely PROPOSED)."
        ),
        journey=Journey.GLOBAL,
        aggregation_type=AggregationType.SUM,
        numerator="SUM(quantity * confirmed_price)",
        denominator=None,
        unit_behavior="CURRENCY_FCFA",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.orders", "marketplace.bids", "marketplace.need_allocations"),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="Status/price columns for all three sources are historically populated.",
    )
)

delivered_gmv = _register(
    MetricDefinition(
        name="delivered_gmv",
        description="Value of need instances that were actually delivered — the only GMV figure that is real revenue, not a commitment.",
        business_definition=(
            "Same formula as confirmed_gmv, additionally gated on the delivered state — DIRECT/"
            "TENDER: Order.delivery_status IN ('DELIVERED','FULFILLED') for order_type != "
            "RECURRING_SUPPLY, else 'RECEIVED' (see module docstring for the full rule and why). "
            "RECURRING: NOT weighted by quantity_delivered (that column has zero writers anywhere "
            "in the codebase — a real gap, see module docstring) — instead gated on the linked "
            "RECURRING_SUPPLY Order reaching 'RECEIVED' via need_allocations -> occurrence.order_"
            "group_id, using the full allocated (not partial) quantity, since there is no reliable "
            "partial-delivery signal today."
        ),
        journey=Journey.GLOBAL,
        aggregation_type=AggregationType.SUM,
        numerator="SUM(quantity * confirmed_price WHERE delivery is RECEIVED/DELIVERED/FULFILLED per the rule above)",
        denominator=None,
        unit_behavior="CURRENCY_FCFA",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.orders", "marketplace.bids", "marketplace.need_allocations"),
        reconstructible_historically=Reconstructibility.PARTIAL,
        reconstructible_note=(
            "YES for DIRECT/TENDER. PARTIAL for RECURRING: depends on the imperfect order_group_id "
            "join (no FK) and cannot reflect partial delivery (quantity_delivered is dead)."
        ),
    )
)


# ---------------------------------------------------------------------------
# DIRECT
# ---------------------------------------------------------------------------

direct_searches = _register(
    MetricDefinition(
        name="direct_searches",
        description="Count of DIRECT product searches performed.",
        business_definition="COUNT of search_products invocations, buyer-scoped.",
        journey=Journey.DIRECT,
        aggregation_type=AggregationType.COUNT,
        numerator="COUNT(search)",
        denominator=None,
        unit_behavior="DIMENSIONLESS_COUNT",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("analytics.business_events (DIRECT_SEARCH_PERFORMED, Phase C)",),
        reconstructible_historically=Reconstructibility.NO,
        reconstructible_note=(
            "No search-log table exists anywhere in the codebase today (confirmed in the Phase A "
            "audit) — this metric has zero history before DIRECT_SEARCH_PERFORMED is instrumented."
        ),
    )
)

direct_search_success_rate = _register(
    MetricDefinition(
        name="direct_search_success_rate",
        description="Share of DIRECT searches that returned at least one result.",
        business_definition="WeightedRate(searches with result_count > 0, all searches).",
        journey=Journey.DIRECT,
        aggregation_type=AggregationType.WEIGHTED_RATIO,
        numerator="SUM(searches with results)",
        denominator="SUM(searches)",
        unit_behavior="DIMENSIONLESS_RATIO",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("analytics.business_events (DIRECT_SEARCH_SUCCEEDED, Phase C)",),
        reconstructible_historically=Reconstructibility.NO,
        reconstructible_note="Depends entirely on direct_searches, itself NO historically.",
    )
)

direct_orders_per_search = _register(
    MetricDefinition(
        name="direct_orders_per_search",
        description="DIRECT orders created per search executed, over the same window.",
        business_definition=(
            "SUM(orders created) / SUM(searches executed) over the window. A WINDOW-LEVEL ratio: there is no "
            "search_id -> cart -> order attribution, so it is NOT a per-session conversion funnel and can exceed 1. "
            "(Renamed from `direct_search_to_order_rate` in Phase D.5 to stop implying attribution.)"
        ),
        journey=Journey.DIRECT,
        aggregation_type=AggregationType.WEIGHTED_RATIO,
        numerator="SUM(orders_created)",
        denominator="SUM(searches)",
        unit_behavior="DIMENSIONLESS_RATIO",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("analytics.business_events (DIRECT_SEARCH_PERFORMED, Phase C)", "marketplace.orders"),
        reconstructible_historically=Reconstructibility.NO,
        reconstructible_note="Depends entirely on direct_searches, itself NO historically.",
    )
)

direct_fulfillment_rate = _register(
    MetricDefinition(
        name="direct_fulfillment_rate",
        description="fulfillment_rate scoped to DIRECT.",
        business_definition=(
            "delivered DIRECT orders / confirmed DIRECT orders (cohort by the order becoming a need). Confirmed = the firm "
            "commitment: Order.status CONFIRMED (producer acceptance or secured escrow payment), evidenced by the "
            "DIRECT_ORDER_CONFIRMED event (Phase D.5) or, before instrumentation, the current status CONFIRMED/COMPLETED/delivered. "
            "A delivered order is always confirmed, so the rate is <= 1."
        ),
        journey=Journey.DIRECT,
        aggregation_type=AggregationType.WEIGHTED_RATIO,
        numerator="SUM(delivered orders)",
        denominator="SUM(confirmed orders)",
        unit_behavior="DIMENSIONLESS_RATIO",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.orders",),
        reconstructible_historically=Reconstructibility.PARTIAL,
        reconstructible_note="Before the Phase D.5 event, an order confirmed and later cancelled is not recoverable (status no longer says it was confirmed): the denominator is then a slight under-count.",
        alias_of="fulfillment_rate",
    )
)

direct_gmv = _register(
    MetricDefinition(
        name="direct_gmv",
        description="confirmed_gmv scoped to DIRECT.",
        business_definition="confirmed_gmv WHERE journey = DIRECT. NOT a 4th kind of GMV — see confirmed_gmv.",
        journey=Journey.DIRECT,
        aggregation_type=AggregationType.SUM,
        numerator="SUM(quantity * confirmed_price)",
        denominator=None,
        unit_behavior="CURRENCY_FCFA",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.orders",),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="Same as confirmed_gmv.",
        alias_of="confirmed_gmv",
    )
)


# ---------------------------------------------------------------------------
# TENDER
# ---------------------------------------------------------------------------

tenders_created = _register(
    MetricDefinition(
        name="tenders_created",
        description="Count of Auctions created.",
        business_definition="COUNT(Auction) in the window.",
        journey=Journey.TENDER,
        aggregation_type=AggregationType.COUNT,
        numerator="COUNT(auction)",
        denominator=None,
        unit_behavior="DIMENSIONLESS_COUNT",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.auctions",),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="Auction.created_at historically populated.",
    )
)

tender_response_rate = _register(
    MetricDefinition(
        name="tender_response_rate",
        description="Share of tenders that received at least one bid.",
        business_definition="WeightedRate(auctions with >=1 bid, auctions created).",
        journey=Journey.TENDER,
        aggregation_type=AggregationType.WEIGHTED_RATIO,
        numerator="SUM(auctions with >=1 bid)",
        denominator="SUM(tenders_created)",
        unit_behavior="DIMENSIONLESS_RATIO",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.auctions", "marketplace.bids"),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="Bid.created_at + Bid.auction_id historically populated.",
    )
)

average_bids_per_tender = _register(
    MetricDefinition(
        name="average_bids_per_tender",
        description="Average number of bids received per tender.",
        business_definition="WeightedRate(total bids, tenders_created) — a weighted average, never an average of per-tender counts across a coarser grouping.",
        journey=Journey.TENDER,
        aggregation_type=AggregationType.WEIGHTED_RATIO,
        numerator="SUM(bids)",
        denominator="SUM(tenders_created)",
        unit_behavior="DIMENSIONLESS_RATIO",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.bids", "marketplace.auctions"),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="Same sources as tender_response_rate.",
    )
)

time_to_first_bid = _register(
    MetricDefinition(
        name="time_to_first_bid",
        description="Average delay between a tender's creation and its first bid.",
        business_definition="SUM(MIN(bid.created_at) - auction.created_at) over auctions with >=1 bid, divided by COUNT(those auctions).",
        journey=Journey.TENDER,
        aggregation_type=AggregationType.DURATION_AVG,
        numerator="SUM(first_bid_at - auction.created_at)",
        denominator="COUNT(auctions with >=1 bid)",
        unit_behavior="DURATION_SECONDS",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.auctions", "marketplace.bids"),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="No dedicated Auction.published_at/first_bid_at column exists — derive first_bid_at as MIN(bid.created_at) GROUP BY auction_id at query time.",
    )
)

tender_winner_rate = _register(
    MetricDefinition(
        name="tender_winner_rate",
        description="Share of tenders that reached a selected winner.",
        business_definition="WeightedRate(auctions with winner_bid_id set, tenders_created).",
        journey=Journey.TENDER,
        aggregation_type=AggregationType.WEIGHTED_RATIO,
        numerator="SUM(auctions with winner_bid_id)",
        denominator="SUM(tenders_created)",
        unit_behavior="DIMENSIONLESS_RATIO",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.auctions",),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="Auction.winner_bid_id/awarded_at historically populated.",
    )
)

tender_fulfillment_rate = _register(
    MetricDefinition(
        name="tender_fulfillment_rate",
        description="fulfillment_rate scoped to TENDER.",
        business_definition="fulfillment_rate WHERE journey = TENDER (confirmed = winner selected, delivered = linked Order delivered).",
        journey=Journey.TENDER,
        aggregation_type=AggregationType.WEIGHTED_RATIO,
        numerator="SUM(delivered auctions)",
        denominator="SUM(auctions with a winner)",
        unit_behavior="DIMENSIONLESS_RATIO",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.auctions", "marketplace.orders"),
        reconstructible_historically=Reconstructibility.PARTIAL,
        reconstructible_note=(
            "PARTIAL: select_winning_bid does not write OrderStatusHistory (Phase A finding), so "
            "the ORDER's own delivery_status must be read directly (fine, it's on the row itself) "
            "— reconstructible, but any future 'time in each status' breakdown for TENDER-born "
            "orders would not be."
        ),
        alias_of="fulfillment_rate",
    )
)

tender_gmv = _register(
    MetricDefinition(
        name="tender_gmv",
        description="confirmed_gmv scoped to TENDER.",
        business_definition="confirmed_gmv WHERE journey = TENDER.",
        journey=Journey.TENDER,
        aggregation_type=AggregationType.SUM,
        numerator="SUM(auction.quantity * winning_bid.offered_price)",
        denominator=None,
        unit_behavior="CURRENCY_FCFA",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.auctions", "marketplace.bids"),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="Same as confirmed_gmv.",
        alias_of="confirmed_gmv",
    )
)


# ---------------------------------------------------------------------------
# RECURRING
# ---------------------------------------------------------------------------

active_recurring_needs = _register(
    MetricDefinition(
        name="active_recurring_needs",
        description="Point-in-time count of standing recurring-need subscriptions.",
        business_definition="COUNT(RecurringNeed WHERE status = 'ACTIVE') as of the reference date — a GAUGE, not a rate over a window.",
        journey=Journey.RECURRING,
        aggregation_type=AggregationType.GAUGE,
        numerator="COUNT(recurring_need)",
        denominator=None,
        unit_behavior="DIMENSIONLESS_COUNT",
        supported_dimensions=("date", "zone", "category", "sub_category"),
        supported_time_windows=("POINT_IN_TIME",),
        source_entities=("marketplace.recurring_needs",),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="RecurringNeed.status/created_at/updated_at historically populated.",
    )
)

recurring_requested_quantity = _register(
    MetricDefinition(
        name="recurring_requested_quantity",
        description="Total quantity requested across recurring occurrences.",
        business_definition="SUM(occurrence.requested_quantity), only across occurrences sharing a compatible canonical unit (see unit_behavior).",
        journey=Journey.RECURRING,
        aggregation_type=AggregationType.SUM,
        numerator="SUM(requested_quantity)",
        denominator=None,
        unit_behavior="ONLY_COMPATIBLE_CANONICAL_UNITS",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.recurring_need_occurrences",),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="requested_quantity is a frozen, never-rewritten column per occurrence.",
    )
)

recurring_matched_quantity = _register(
    MetricDefinition(
        name="recurring_matched_quantity",
        description="Total quantity matched across recurring occurrences.",
        business_definition="SUM(occurrence.quantity_matched), same unit-compatibility rule as recurring_requested_quantity.",
        journey=Journey.RECURRING,
        aggregation_type=AggregationType.SUM,
        numerator="SUM(quantity_matched)",
        denominator=None,
        unit_behavior="ONLY_COMPATIBLE_CANONICAL_UNITS",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.recurring_need_occurrences",),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="quantity_matched is written by rematch_occurrence and historically populated.",
    )
)

recurring_confirmed_quantity = _register(
    MetricDefinition(
        name="recurring_confirmed_quantity",
        description="Total quantity confirmed (buyer-accepted) across recurring occurrences.",
        business_definition="SUM(occurrence.quantity_confirmed).",
        journey=Journey.RECURRING,
        aggregation_type=AggregationType.SUM,
        numerator="SUM(quantity_confirmed)",
        denominator=None,
        unit_behavior="ONLY_COMPATIBLE_CANONICAL_UNITS",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.recurring_need_occurrences",),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="quantity_confirmed is written by accept_match_proposal and historically populated.",
    )
)

recurring_delivered_quantity = _register(
    MetricDefinition(
        name="recurring_delivered_quantity",
        description="Quantity of recurring supply confirmed RECEIVED by the buyer.",
        business_definition=(
            "SUM(occurrence.quantity_delivered) where quantity_delivered = SUM(OrderItem.quantity) of the occurrence's CONVERTED "
            "allocations whose RECURRING_SUPPLY order is delivery_status = RECEIVED (Phase D.5: the column now has a writer, "
            "recomputed - never incremented - in the transaction of the RECEIVED transition). RECEIVED_WITH_ISSUE is not counted "
            "(its received quantity is only free text): a lower bound."
        ),
        journey=Journey.RECURRING,
        aggregation_type=AggregationType.SUM,
        numerator="SUM(quantity_delivered)",
        denominator=None,
        unit_behavior="ONLY_COMPATIBLE_CANONICAL_UNITS",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.recurring_need_occurrences", "marketplace.need_allocations", "marketplace.order_items", "marketplace.orders"),
        reconstructible_historically=Reconstructibility.PARTIAL,
        reconstructible_note="Aggregates read the RECEIVED-order join directly, so history is recomputable; it still depends on the buyer's RECEIVED confirmation.",
    )
)

recurring_coverage_rate = _register(
    MetricDefinition(
        name="recurring_coverage_rate",
        description="Share of requested recurring quantity that was matched.",
        business_definition="WeightedRate(SUM(quantity_matched), SUM(requested_quantity)) — mission's own worked example: 425 KG requested / 100 KG matched = 23.529...%, never averaged per-occurrence percentages.",
        journey=Journey.RECURRING,
        aggregation_type=AggregationType.WEIGHTED_RATIO,
        numerator="SUM(quantity_matched)",
        denominator="SUM(requested_quantity)",
        unit_behavior="ONLY_COMPATIBLE_CANONICAL_UNITS",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.recurring_need_occurrences",),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="Both columns historically populated and already unit-compatible by construction (same occurrence).",
    )
)

recurring_full_coverage_rate = _register(
    MetricDefinition(
        name="recurring_full_coverage_rate",
        description="Share of recurring occurrences that were matched IN FULL (not partially).",
        business_definition="WeightedRate(COUNT(occurrences WHERE quantity_matched >= requested_quantity), COUNT(occurrences)) — a count-ratio, distinct from recurring_coverage_rate's quantity-ratio (an occurrence can be 90% covered and still count as 0 here).",
        journey=Journey.RECURRING,
        aggregation_type=AggregationType.WEIGHTED_RATIO,
        numerator="COUNT(fully matched occurrences)",
        denominator="COUNT(occurrences)",
        unit_behavior="DIMENSIONLESS_RATIO",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.recurring_need_occurrences",),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="Derived from the same historically-populated columns as recurring_coverage_rate.",
    )
)

recurring_acceptance_rate = _register(
    MetricDefinition(
        name="recurring_acceptance_rate",
        description="Of digested occurrences, the share the buyer accepted (fully or partially).",
        business_definition="WeightedRate(COUNT(occurrences WHERE status IN ('ACCEPTED','PARTIALLY_ACCEPTED')), COUNT(occurrences WHERE notified_at IS NOT NULL)) — population is 'was digested', not 'was matched', since an occurrence can match without a buyer response yet.",
        journey=Journey.RECURRING,
        aggregation_type=AggregationType.WEIGHTED_RATIO,
        numerator="COUNT(occurrences ACCEPTED or PARTIALLY_ACCEPTED)",
        denominator="COUNT(occurrences with notified_at set)",
        unit_behavior="DIMENSIONLESS_RATIO",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.recurring_need_occurrences",),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="status + notified_at historically populated.",
    )
)

recurring_modification_rate = _register(
    MetricDefinition(
        name="recurring_modification_rate",
        description="Of digested occurrences, the share that triggered a modify action (quantity/frequency/pause/occurrence override) instead of a plain accept/reject.",
        business_definition="WeightedRate(COUNT(digested occurrences whose need received an update_recurring_need modify action within the same digest cycle), COUNT(occurrences with notified_at set)).",
        journey=Journey.RECURRING,
        aggregation_type=AggregationType.WEIGHTED_RATIO,
        numerator="COUNT(digested occurrences with a correlated modify action)",
        denominator="COUNT(occurrences with notified_at set)",
        unit_behavior="DIMENSIONLESS_RATIO",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.recurring_need_occurrences", "marketplace.recurring_needs"),
        reconstructible_historically=Reconstructibility.PARTIAL,
        reconstructible_note=(
            "PARTIAL: update_recurring_need's actions are recorded on the RecurringNeed/occurrence "
            "rows themselves (e.g. OCCURRENCE_OVERRIDE/status), but there is no single 'buyer_response' "
            "log correlating a modify action back to the specific digest cycle that prompted it — "
            "correlation by timestamp proximity is an approximation, not exact, for historical data."
        ),
    )
)

recurring_skip_rate = _register(
    MetricDefinition(
        name="recurring_skip_rate",
        description="Share of the day's recurring occurrences the buyer explicitly skipped.",
        business_definition="WeightedRate(COUNT(occurrences WHERE status = 'SKIPPED'), COUNT(occurrences)) over the occurrences of the window (Phase D: relevant occurrences = all non-deleted ones; a skip can happen before any digest).",
        journey=Journey.RECURRING,
        aggregation_type=AggregationType.WEIGHTED_RATIO,
        numerator="COUNT(occurrences SKIPPED)",
        denominator="COUNT(occurrences)",
        unit_behavior="DIMENSIONLESS_RATIO",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.recurring_need_occurrences",),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="status historically populated.",
    )
)

recurring_fulfillment_rate = _register(
    MetricDefinition(
        name="recurring_fulfillment_rate",
        description="Share of confirmed recurring quantity that the buyer confirmed RECEIVED.",
        business_definition=(
            "SUM(quantity_delivered) / SUM(quantity_confirmed), per canonical unit. Phase D.5 changed the Phase B formula "
            "(occurrence COUNT of linked orders RECEIVED / occurrences with quantity_confirmed > 0) to a quantity ratio now that "
            "quantity_delivered is exact; the occurrence-count view remains available as `recurring_received_occurrence_rate`. "
            "Lower bound: a buyer who never confirms reception reads as undelivered."
        ),
        journey=Journey.RECURRING,
        aggregation_type=AggregationType.WEIGHTED_RATIO,
        numerator="SUM(quantity_delivered)",
        denominator="SUM(quantity_confirmed)",
        unit_behavior="ONLY_COMPATIBLE_CANONICAL_UNITS",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.recurring_need_occurrences", "marketplace.need_allocations", "marketplace.orders"),
        reconstructible_historically=Reconstructibility.PARTIAL,
        reconstructible_note="Exact join by construction (allocation.order_item_id), but needs the buyer's RECEIVED confirmation.",
    )
)

recurring_gmv = _register(
    MetricDefinition(
        name="recurring_gmv",
        description="confirmed_gmv scoped to RECURRING.",
        business_definition="confirmed_gmv WHERE journey = RECURRING.",
        journey=Journey.RECURRING,
        aggregation_type=AggregationType.SUM,
        numerator="SUM(need_allocations.quantity * need_allocations.unit_price WHERE status='ACCEPTED')",
        denominator=None,
        unit_behavior="CURRENCY_FCFA",
        supported_dimensions=_STANDARD_DIMENSIONS,
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.need_allocations",),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="Same as confirmed_gmv.",
        alias_of="confirmed_gmv",
    )
)


# ---------------------------------------------------------------------------
# Phase D additions — honest replacements for metrics whose dictionary definition
# depends on data that does not exist (see metric_layer.UNAVAILABLE).
# ---------------------------------------------------------------------------

direct_order_delivery_rate = _register(
    MetricDefinition(
        name="direct_order_delivery_rate",
        description="Of DIRECT orders created in the window, the share that reached delivery.",
        business_definition="WeightedRate(COUNT(orders delivery_status IN (DELIVERED, FULFILLED)), COUNT(orders created)) — cohort by creation date. NOT `direct_fulfillment_rate` (delivered/confirmed): confirmation is not instrumented.",
        journey=Journey.DIRECT,
        aggregation_type=AggregationType.WEIGHTED_RATIO,
        numerator="SUM(orders_delivered)",
        denominator="SUM(orders_created)",
        unit_behavior="DIMENSIONLESS_RATIO",
        supported_dimensions=("date", "zone", "category", "sub_category"),
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.orders",),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="Order.created_at + current delivery_status; every historical day is recomputable.",
    )
)

direct_orders_created = _register(
    MetricDefinition(
        name="direct_orders_created",
        description="DIRECT (catalog cart) orders created in the window.",
        business_definition="COUNT(orders WHERE auction_id IS NULL, order_type = STANDARD, buyer known, status NOT IN (DRAFT, SUPERSEDED)).",
        journey=Journey.DIRECT,
        aggregation_type=AggregationType.COUNT,
        numerator="SUM(orders_created)",
        denominator=None,
        unit_behavior="DIMENSIONLESS_COUNT",
        supported_dimensions=("date", "zone", "category", "sub_category"),
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.orders",),
        reconstructible_historically=Reconstructibility.YES,
        reconstructible_note="Order.created_at.",
    )
)

direct_orders_confirmed = _register(
    MetricDefinition(
        name="direct_orders_confirmed",
        description="DIRECT orders that reached the firm commitment (Order.status CONFIRMED).",
        business_definition="COUNT(DIRECT orders WHERE DIRECT_ORDER_CONFIRMED was emitted OR status IN (CONFIRMED, COMPLETED) OR delivered). Confirmed = producer acceptance (confirm_order_by_producer) or secured escrow payment (mark_escrow_paid).",
        journey=Journey.DIRECT,
        aggregation_type=AggregationType.COUNT,
        numerator="SUM(orders_confirmed)",
        denominator=None,
        unit_behavior="DIMENSIONLESS_COUNT",
        supported_dimensions=("date", "zone", "category", "sub_category"),
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.orders", "analytics.business_events (DIRECT_ORDER_CONFIRMED, Phase D.5)"),
        reconstructible_historically=Reconstructibility.PARTIAL,
        reconstructible_note="Current status recovers confirmed orders that were not cancelled afterwards.",
    )
)

recurring_unmatched_quantity = _register(
    MetricDefinition(
        name="recurring_unmatched_quantity",
        description="Recurring demand the matching engine could not cover (requested - matched).",
        business_definition="SUM(GREATEST(requested_quantity - quantity_matched, 0)) over non-withdrawn occurrences, per canonical unit. UNMATCHED demand only — never 'undelivered' demand.",
        journey=Journey.RECURRING,
        aggregation_type=AggregationType.SUM,
        numerator="SUM(unmatched_quantity)",
        denominator=None,
        unit_behavior="ONLY_COMPATIBLE_CANONICAL_UNITS",
        supported_dimensions=("date", "zone", "category", "sub_category"),
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.recurring_need_occurrences",),
        reconstructible_historically=Reconstructibility.PARTIAL,
        reconstructible_note="quantity_matched is the CURRENT matching state (rematching overwrites it), not a historical snapshot of what was matched that day.",
    )
)

recurring_received_occurrence_rate = _register(
    MetricDefinition(
        name="recurring_received_occurrence_rate",
        description="Of accepted recurring occurrences, the share whose orders were all confirmed RECEIVED by the buyer.",
        business_definition="WeightedRate(COUNT(occurrences whose RECURRING_SUPPLY orders (checkout_group_id = occurrence.order_group_id) are ALL delivery_status RECEIVED), COUNT(occurrences ACCEPTED or PARTIALLY_ACCEPTED)). Occurrence-level; a lower bound because non-response is not receipt.",
        journey=Journey.RECURRING,
        aggregation_type=AggregationType.WEIGHTED_RATIO,
        numerator="SUM(occurrences_all_received)",
        denominator="SUM(occurrences_accepted)",
        unit_behavior="DIMENSIONLESS_RATIO",
        supported_dimensions=("date", "zone", "category", "sub_category"),
        supported_time_windows=_STANDARD_WINDOWS,
        source_entities=("marketplace.recurring_need_occurrences", "marketplace.orders"),
        reconstructible_historically=Reconstructibility.PARTIAL,
        reconstructible_note="Exact join by construction (order_group_id and checkout_group_id are written in the same transaction) but requires the buyer's RECEIVED confirmation.",
    )
)


# Phase E — plain counts the cockpit shows next to the rates (numerators/denominators already stored).
def _register_count(name, journey, column, description, source):
    return _register(
        MetricDefinition(
            name=name,
            description=description,
            business_definition=description,
            journey=journey,
            aggregation_type=AggregationType.COUNT,
            numerator=f"SUM({column})",
            denominator=None,
            unit_behavior="DIMENSIONLESS_COUNT",
            supported_dimensions=("date", "zone", "category", "sub_category"),
            supported_time_windows=_STANDARD_WINDOWS,
            source_entities=source,
            reconstructible_historically=Reconstructibility.PARTIAL,
            reconstructible_note="Daily aggregate of the underlying transactional state (see METRIC_LAYER.md).",
        )
    )


direct_successful_searches = _register_count(
    "direct_successful_searches", Journey.DIRECT, "successful_searches",
    "DIRECT searches that returned at least one eligible result.", ("analytics.business_events",))
direct_orders_delivered = _register_count(
    "direct_orders_delivered", Journey.DIRECT, "orders_delivered",
    "DIRECT orders (cohort) that reached delivery.", ("marketplace.orders",))
tenders_with_bid = _register_count(
    "tenders_with_bid", Journey.TENDER, "tenders_with_bid", "Tenders that received at least one bid.", ("marketplace.auctions", "marketplace.bids"))
tender_orders_created = _register_count(
    "tender_orders_created", Journey.TENDER, "tender_orders_created", "Orders created from tender winners.", ("marketplace.orders",))
tender_orders_delivered = _register_count(
    "tender_orders_delivered", Journey.TENDER, "tender_orders_delivered", "Tender orders that reached delivery.", ("marketplace.orders",))
recurring_occurrences = _register_count(
    "recurring_occurrences", Journey.RECURRING, "occurrences_active",
    "Recurring occurrences the buyer maintains (not skipped/cancelled) on the demand dates of the window.",
    ("marketplace.recurring_need_occurrences",))


def get_metric(name: str) -> MetricDefinition:
    try:
        return METRICS[name]
    except KeyError as exc:
        raise KeyError(f"Unknown metric {name!r} — not in the buyer metric dictionary.") from exc


def list_metrics_for_journey(journey: Journey) -> tuple[MetricDefinition, ...]:
    return tuple(m for m in METRICS.values() if m.journey == journey)


__all__ = [
    "Journey",
    "AggregationType",
    "Reconstructibility",
    "MetricDefinition",
    "METRICS",
    "get_metric",
    "list_metrics_for_journey",
]
