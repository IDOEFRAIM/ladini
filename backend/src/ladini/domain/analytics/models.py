"""SQLAlchemy mirror of the `analytics` schema (Phase C).

Source of truth: Drizzle (`src/db/schema/analytics.ts`, frontend repo) — these
classes are its exact mirror, verified by `tests/schema/`. No table creation
happens here; the Drizzle migration is the only source of DDL.

Named `*Record` to stay distinct from this package's Phase B dataclasses
(`business_events.py::BusinessEvent`, `metric_targets.py::MetricTarget`),
which are in-memory validated contracts, not ORM rows — an emitter
constructs the dataclass first (validation), then persists it as the
matching `*Record` row.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from ladini.domain.orm_base import Base, _uuid4


def _tz() -> DateTime:
    return DateTime(timezone=True)


_EVENT_NAMES_SQL = (
    "'DIRECT_SEARCH_PERFORMED','DIRECT_SEARCH_SUCCEEDED','DIRECT_ORDER_CREATED',"
    "'DIRECT_ORDER_CONFIRMED','DIRECT_ORDER_DELIVERED','DIRECT_ORDER_FAILED',"
    "'TENDER_CREATED','TENDER_PUBLISHED','TENDER_BID_RECEIVED','TENDER_WINNER_SELECTED',"
    "'TENDER_ORDER_CREATED','TENDER_DELIVERED',"
    "'RECURRING_NEED_CREATED','RECURRING_OCCURRENCE_CREATED','RECURRING_MATCH_FOUND',"
    "'RECURRING_DIGEST_SENT','RECURRING_DIGEST_ACCEPTED','RECURRING_DIGEST_MODIFIED',"
    "'RECURRING_OCCURRENCE_SKIPPED','RECURRING_OCCURRENCE_CONFIRMED','RECURRING_OCCURRENCE_DELIVERED',"
    "'PRODUCT_PUBLISHED_FOR_SALE','PRODUCT_SELLABLE_QUANTITY_CHANGED'"
)
_JOURNEY_VALUES_SQL = "'DIRECT','TENDER','RECURRING','SUPPLY'"


class EventOutboxRecord(Base):
    """Transactional-outbox intent row — written in the SAME session/
    transaction as the business action (same discipline as
    `intelligence.notification_outbox`), drained asynchronously by
    `workers/crons/analytics_event_drain.py` into `BusinessEventRecord`."""

    __tablename__ = "event_outbox"
    __table_args__ = (
        Index("event_outbox_dedupe_key_uq", "dedupe_key", unique=True),
        Index("event_outbox_claim_idx", "status", "next_attempt_at"),
        CheckConstraint(
            "status IN ('PENDING','SENDING','SENT','FAILED','DEAD')",
            name="event_outbox_status_chk",
        ),
        CheckConstraint(f"journey IN ({_JOURNEY_VALUES_SQL})", name="event_outbox_journey_chk"),
        {"schema": "analytics"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    event_name = Column(Text, nullable=False)
    journey = Column(Text, nullable=False)
    payload = Column(JSONB, nullable=False)
    dedupe_key = Column(Text, nullable=False)
    status = Column(Text, nullable=False, server_default=text("'PENDING'"))
    attempts = Column(Integer, nullable=False, server_default=text("0"))
    last_error = Column(Text)
    next_attempt_at = Column(_tz(), server_default=func.now(), nullable=False)
    created_at = Column(_tz(), server_default=func.now(), nullable=False)


class BusinessEventRecord(Base):
    """The durable, append-only business fact. Never written directly by
    business-flow code — only by the outbox drain worker, from a validated
    `business_events.py::BusinessEvent` dataclass."""

    __tablename__ = "business_events"
    __table_args__ = (
        Index("business_events_idempotency_key_uq", "idempotency_key", unique=True),
        Index("business_events_event_name_idx", "event_name", "occurred_at"),
        Index("business_events_journey_idx", "journey", "occurred_at"),
        Index("business_events_occurred_at_idx", "occurred_at"),
        Index("business_events_buyer_idx", "buyer_id", "occurred_at"),
        Index("business_events_entity_idx", "entity_type", "entity_id"),
        Index("business_events_sub_category_idx", "sub_category_id", "occurred_at"),
        Index("business_events_zone_idx", "zone_id", "occurred_at"),
        CheckConstraint(f"event_name IN ({_EVENT_NAMES_SQL})", name="business_events_event_name_chk"),
        CheckConstraint(f"journey IN ({_JOURNEY_VALUES_SQL})", name="business_events_journey_chk"),
        CheckConstraint(
            "actor_type IN ('BUYER','PRODUCER','SYSTEM','ADMIN')",
            name="business_events_actor_type_chk",
        ),
        CheckConstraint(
            "measurement_family IS NULL OR measurement_family IN ('MASS','VOLUME','COUNT','PACKAGE','OTHER')",
            name="business_events_measurement_family_chk",
        ),
        CheckConstraint(
            "quantity IS NULL OR quantity >= 0", name="business_events_quantity_non_negative_chk"
        ),
        CheckConstraint("amount IS NULL OR amount >= 0", name="business_events_amount_non_negative_chk"),
        {"schema": "analytics"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))

    event_name = Column(Text, nullable=False)
    journey = Column(Text, nullable=False)

    actor_type = Column(Text, nullable=False)
    actor_id = Column(PG_UUID(as_uuid=True))

    buyer_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.buyer_profiles.id", ondelete="SET NULL"))
    producer_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.producers.id", ondelete="SET NULL"))

    entity_type = Column(Text, nullable=False)
    entity_id = Column(PG_UUID(as_uuid=True), nullable=False)

    category_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.categories.id", ondelete="SET NULL"))
    sub_category_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.sub_categories.id", ondelete="SET NULL"))
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id", ondelete="SET NULL"))

    quantity = Column(Numeric(14, 3))
    unit = Column(Text)
    canonical_quantity = Column(Numeric(14, 3))
    canonical_unit = Column(Text)
    measurement_family = Column(Text)

    amount = Column(Numeric(14, 2))
    currency = Column(Text, nullable=False, server_default=text("'XOF'"))

    metadata_ = Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    occurred_at = Column(_tz(), nullable=False)
    created_at = Column(_tz(), server_default=func.now(), nullable=False)

    idempotency_key = Column(Text, nullable=False)


class MetricTargetRecord(Base):
    __tablename__ = "metric_targets"
    __table_args__ = (
        Index("metric_targets_metric_scope_idx", "metric_name", "scope_type", "scope_id"),
        CheckConstraint(
            "scope_type IN ('GLOBAL','JOURNEY','CATEGORY','SUBCATEGORY','ZONE')",
            name="metric_targets_scope_type_chk",
        ),
        CheckConstraint(
            "(scope_type = 'GLOBAL' AND scope_id IS NULL) OR (scope_type <> 'GLOBAL' AND scope_id IS NOT NULL)",
            name="metric_targets_scope_id_matches_scope_type_chk",
        ),
        {"schema": "analytics"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))

    metric_name = Column(Text, nullable=False)
    scope_type = Column(Text, nullable=False)
    scope_id = Column(PG_UUID(as_uuid=True))

    target_value = Column(Numeric(10, 4), nullable=False)
    warning_threshold = Column(Numeric(10, 4))
    critical_threshold = Column(Numeric(10, 4))

    valid_from = Column(Date, nullable=False)
    valid_until = Column(Date)

    created_at = Column(_tz(), server_default=func.now(), nullable=False)
    updated_at = Column(_tz(), server_default=func.now(), onupdate=func.now(), nullable=False)


# ---------------------------------------------------------------------------
# Phase D — daily aggregate tables (mirror of `analytics.ts`, Drizzle = source of truth).
# Derived + rebuildable, NO foreign keys. Dimensions default to the nil UUID
# ("not attributable") instead of NULL so the UNIQUE grain index is real.
# ---------------------------------------------------------------------------

class BuyerDailyMetricRecord(Base):
    __tablename__ = "buyer_daily_metrics"
    __table_args__ = (
        Index("buyer_daily_metrics_grain_uq", "metric_date", "buyer_id", unique=True),
        Index("buyer_daily_metrics_date_zone_idx", "metric_date", "zone_id"),
        CheckConstraint("confirmed_gmv_direct >= 0", name="buyer_daily_metrics_confirmed_direct_chk"),
        CheckConstraint("needs_direct >= 0 AND needs_tender >= 0 AND needs_recurring >= 0 AND satisfied_direct >= 0 AND satisfied_tender >= 0 AND satisfied_recurring >= 0 AND digests_queued >= 0 AND digests_accepted >= 0", name="buyer_daily_metrics_counts_chk"),
        CheckConstraint("potential_gmv_direct >= 0 AND potential_gmv_tender >= 0 AND potential_gmv_recurring >= 0 AND confirmed_gmv_tender >= 0 AND confirmed_gmv_recurring >= 0 AND delivered_gmv_direct >= 0 AND delivered_gmv_tender >= 0 AND delivered_gmv_recurring >= 0", name="buyer_daily_metrics_money_chk"),
        {"schema": "analytics"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    metric_date = Column(Date, nullable=False)
    buyer_id = Column(PG_UUID(as_uuid=True), nullable=False)
    zone_id = Column(PG_UUID(as_uuid=True), nullable=False, server_default=text("'00000000-0000-0000-0000-000000000000'"))
    needs_direct = Column(Integer, nullable=False, server_default=text("0"))
    needs_tender = Column(Integer, nullable=False, server_default=text("0"))
    needs_recurring = Column(Integer, nullable=False, server_default=text("0"))
    satisfied_direct = Column(Integer, nullable=False, server_default=text("0"))
    satisfied_tender = Column(Integer, nullable=False, server_default=text("0"))
    satisfied_recurring = Column(Integer, nullable=False, server_default=text("0"))
    potential_gmv_direct = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    potential_gmv_tender = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    potential_gmv_recurring = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    confirmed_gmv_direct = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    confirmed_gmv_tender = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    confirmed_gmv_recurring = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    delivered_gmv_direct = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    delivered_gmv_tender = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    delivered_gmv_recurring = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    digests_queued = Column(Integer, nullable=False, server_default=text("0"))
    digests_accepted = Column(Integer, nullable=False, server_default=text("0"))
    computed_at = Column(_tz(), server_default=func.now(), nullable=False)


class DirectDailyMetricRecord(Base):
    __tablename__ = "direct_daily_metrics"
    __table_args__ = (
        Index("direct_daily_metrics_grain_uq", "metric_date", "zone_id", "category_id", "sub_category_id", unique=True),
        Index("direct_daily_metrics_date_idx", "metric_date"),
        CheckConstraint("orders_confirmed >= 0 AND confirmed_value >= 0", name="direct_daily_metrics_confirmed_chk"),
        CheckConstraint("searches >= 0 AND successful_searches >= 0 AND orders_created >= 0 AND orders_delivered >= 0 AND created_value >= 0 AND delivered_value >= 0", name="direct_daily_metrics_counts_chk"),
        {"schema": "analytics"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    metric_date = Column(Date, nullable=False)
    zone_id = Column(PG_UUID(as_uuid=True), nullable=False, server_default=text("'00000000-0000-0000-0000-000000000000'"))
    category_id = Column(PG_UUID(as_uuid=True), nullable=False, server_default=text("'00000000-0000-0000-0000-000000000000'"))
    sub_category_id = Column(PG_UUID(as_uuid=True), nullable=False, server_default=text("'00000000-0000-0000-0000-000000000000'"))
    searches = Column(Integer, nullable=False, server_default=text("0"))
    successful_searches = Column(Integer, nullable=False, server_default=text("0"))
    orders_created = Column(Integer, nullable=False, server_default=text("0"))
    orders_delivered = Column(Integer, nullable=False, server_default=text("0"))
    orders_confirmed = Column(Integer, nullable=False, server_default=text("0"))
    confirmed_value = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    created_value = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    delivered_value = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    computed_at = Column(_tz(), server_default=func.now(), nullable=False)


class TenderDailyMetricRecord(Base):
    __tablename__ = "tender_daily_metrics"
    __table_args__ = (
        Index("tender_daily_metrics_grain_uq", "metric_date", "zone_id", "category_id", "sub_category_id", unique=True),
        Index("tender_daily_metrics_date_idx", "metric_date"),
        CheckConstraint("tenders_created >= 0 AND tenders_with_bid >= 0 AND bids_received >= 0 AND tenders_with_winner >= 0 AND tender_orders_created >= 0 AND tender_orders_delivered >= 0 AND first_bid_latency_seconds_sum >= 0 AND first_bid_latency_count >= 0 AND potential_value >= 0 AND committed_value >= 0 AND delivered_value >= 0", name="tender_daily_metrics_counts_chk"),
        {"schema": "analytics"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    metric_date = Column(Date, nullable=False)
    zone_id = Column(PG_UUID(as_uuid=True), nullable=False, server_default=text("'00000000-0000-0000-0000-000000000000'"))
    category_id = Column(PG_UUID(as_uuid=True), nullable=False, server_default=text("'00000000-0000-0000-0000-000000000000'"))
    sub_category_id = Column(PG_UUID(as_uuid=True), nullable=False, server_default=text("'00000000-0000-0000-0000-000000000000'"))
    tenders_created = Column(Integer, nullable=False, server_default=text("0"))
    tenders_with_bid = Column(Integer, nullable=False, server_default=text("0"))
    bids_received = Column(Integer, nullable=False, server_default=text("0"))
    tenders_with_winner = Column(Integer, nullable=False, server_default=text("0"))
    tender_orders_created = Column(Integer, nullable=False, server_default=text("0"))
    tender_orders_delivered = Column(Integer, nullable=False, server_default=text("0"))
    first_bid_latency_seconds_sum = Column(Numeric(18, 3), nullable=False, server_default=text("'0'"))
    first_bid_latency_count = Column(Integer, nullable=False, server_default=text("0"))
    potential_value = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    committed_value = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    delivered_value = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    computed_at = Column(_tz(), server_default=func.now(), nullable=False)


class RecurringDailyMetricRecord(Base):
    __tablename__ = "recurring_daily_metrics"
    __table_args__ = (
        Index("recurring_daily_metrics_grain_uq", "metric_date", "zone_id", "category_id", "sub_category_id", "canonical_unit", unique=True),
        Index("recurring_daily_metrics_date_idx", "metric_date"),
        CheckConstraint("delivered_quantity >= 0", name="recurring_daily_metrics_delivered_chk"),
        CheckConstraint("measurement_family IN ('MASS','VOLUME','COUNT','PACKAGE','OTHER')", name="recurring_daily_metrics_family_chk"),
        CheckConstraint("occurrences_total >= 0 AND occurrences_active >= 0 AND occurrences_fully_covered >= 0 AND occurrences_notified >= 0 AND occurrences_accepted >= 0 AND occurrences_skipped >= 0 AND occurrences_with_orders >= 0 AND occurrences_all_received >= 0 AND needs_with_occurrence >= 0", name="recurring_daily_metrics_counts_chk"),
        CheckConstraint("requested_quantity >= 0 AND matched_quantity >= 0 AND confirmed_quantity >= 0 AND unmatched_quantity >= 0 AND potential_value >= 0 AND confirmed_value >= 0 AND received_value >= 0", name="recurring_daily_metrics_qty_chk"),
        {"schema": "analytics"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    metric_date = Column(Date, nullable=False)
    zone_id = Column(PG_UUID(as_uuid=True), nullable=False, server_default=text("'00000000-0000-0000-0000-000000000000'"))
    category_id = Column(PG_UUID(as_uuid=True), nullable=False, server_default=text("'00000000-0000-0000-0000-000000000000'"))
    sub_category_id = Column(PG_UUID(as_uuid=True), nullable=False, server_default=text("'00000000-0000-0000-0000-000000000000'"))
    canonical_unit = Column(Text, nullable=False)
    measurement_family = Column(Text, nullable=False)
    occurrences_total = Column(Integer, nullable=False, server_default=text("0"))
    occurrences_active = Column(Integer, nullable=False, server_default=text("0"))
    occurrences_fully_covered = Column(Integer, nullable=False, server_default=text("0"))
    occurrences_notified = Column(Integer, nullable=False, server_default=text("0"))
    occurrences_accepted = Column(Integer, nullable=False, server_default=text("0"))
    occurrences_skipped = Column(Integer, nullable=False, server_default=text("0"))
    occurrences_with_orders = Column(Integer, nullable=False, server_default=text("0"))
    occurrences_all_received = Column(Integer, nullable=False, server_default=text("0"))
    needs_with_occurrence = Column(Integer, nullable=False, server_default=text("0"))
    requested_quantity = Column(Numeric(16, 3), nullable=False, server_default=text("'0'"))
    matched_quantity = Column(Numeric(16, 3), nullable=False, server_default=text("'0'"))
    confirmed_quantity = Column(Numeric(16, 3), nullable=False, server_default=text("'0'"))
    delivered_quantity = Column(Numeric(16, 3), nullable=False, server_default=text("'0'"))
    unmatched_quantity = Column(Numeric(16, 3), nullable=False, server_default=text("'0'"))
    potential_value = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    confirmed_value = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    received_value = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    computed_at = Column(_tz(), server_default=func.now(), nullable=False)


# ---------------------------------------------------------------------------
# Producer Analytics Phase C — daily aggregate tables (mirror of `analytics.ts`,
# Drizzle = source of truth). Same discipline as the buyer tables above: derived
# + rebuildable, NO foreign keys, dimensions default to the nil UUID.
# ---------------------------------------------------------------------------


class ProducerDailyMetricRecord(Base):
    """Grain (metric_date, producer_id). `zone_id` is the PRODUCER's own zone
    (Producer.zone_id), never the buyer/delivery zone. A row only exists when
    the producer had >= 1 qualifying fact that day (publish, quantity change,
    bid received, order confirmed/delivered) — so COUNT(DISTINCT producer_id)
    over a window is exactly "active producers", no separate flag needed."""

    __tablename__ = "producer_daily_metrics"
    __table_args__ = (
        Index("producer_daily_metrics_grain_uq", "metric_date", "producer_id", unique=True),
        Index("producer_daily_metrics_date_zone_idx", "metric_date", "zone_id"),
        CheckConstraint(
            "products_published >= 0 AND quantity_changes >= 0 AND bids_received >= 0 AND "
            "orders_confirmed_direct >= 0 AND orders_delivered_direct >= 0 AND "
            "orders_confirmed_tender >= 0 AND orders_delivered_tender >= 0 AND "
            "orders_confirmed_recurring >= 0 AND orders_delivered_recurring >= 0",
            name="producer_daily_metrics_counts_chk",
        ),
        CheckConstraint(
            "delivered_gmv_direct >= 0 AND delivered_gmv_tender >= 0 AND delivered_gmv_recurring >= 0",
            name="producer_daily_metrics_money_chk",
        ),
        {"schema": "analytics"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    metric_date = Column(Date, nullable=False)
    producer_id = Column(PG_UUID(as_uuid=True), nullable=False)
    zone_id = Column(PG_UUID(as_uuid=True), nullable=False, server_default=text("'00000000-0000-0000-0000-000000000000'"))
    products_published = Column(Integer, nullable=False, server_default=text("0"))
    quantity_changes = Column(Integer, nullable=False, server_default=text("0"))
    bids_received = Column(Integer, nullable=False, server_default=text("0"))
    orders_confirmed_direct = Column(Integer, nullable=False, server_default=text("0"))
    orders_delivered_direct = Column(Integer, nullable=False, server_default=text("0"))
    orders_confirmed_tender = Column(Integer, nullable=False, server_default=text("0"))
    orders_delivered_tender = Column(Integer, nullable=False, server_default=text("0"))
    orders_confirmed_recurring = Column(Integer, nullable=False, server_default=text("0"))
    orders_delivered_recurring = Column(Integer, nullable=False, server_default=text("0"))
    delivered_gmv_direct = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    delivered_gmv_tender = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    delivered_gmv_recurring = Column(Numeric(16, 2), nullable=False, server_default=text("'0'"))
    computed_at = Column(_tz(), server_default=func.now(), nullable=False)


class ProducerQuantityDailyMetricRecord(Base):
    """Grain (metric_date, producer_id, canonical_unit) — never KG + L + TETE in
    one sum. TENDER has no columns here at all (no OrderItem exists for a TENDER
    order, so no reliable quantity — see PRODUCER_ANALYTICS_ARCHITECTURE.md
    §3.2/§10): deliberately absent, not always-zero."""

    __tablename__ = "producer_quantity_daily_metrics"
    __table_args__ = (
        Index("producer_quantity_daily_metrics_grain_uq", "metric_date", "producer_id", "canonical_unit", unique=True),
        Index("producer_quantity_daily_metrics_date_idx", "metric_date"),
        CheckConstraint("measurement_family IN ('MASS','VOLUME','COUNT','PACKAGE','OTHER')", name="producer_quantity_daily_metrics_family_chk"),
        CheckConstraint(
            "confirmed_quantity_direct >= 0 AND delivered_quantity_direct >= 0 AND "
            "confirmed_quantity_recurring >= 0 AND delivered_quantity_recurring >= 0",
            name="producer_quantity_daily_metrics_qty_chk",
        ),
        {"schema": "analytics"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    metric_date = Column(Date, nullable=False)
    producer_id = Column(PG_UUID(as_uuid=True), nullable=False)
    canonical_unit = Column(Text, nullable=False)
    measurement_family = Column(Text, nullable=False)
    confirmed_quantity_direct = Column(Numeric(16, 3), nullable=False, server_default=text("'0'"))
    delivered_quantity_direct = Column(Numeric(16, 3), nullable=False, server_default=text("'0'"))
    confirmed_quantity_recurring = Column(Numeric(16, 3), nullable=False, server_default=text("'0'"))
    delivered_quantity_recurring = Column(Numeric(16, 3), nullable=False, server_default=text("'0'"))
    computed_at = Column(_tz(), server_default=func.now(), nullable=False)


class ProducerSupplyDailySnapshotRecord(Base):
    """Grain (metric_date, producer_id, zone_id, category_id, sub_category_id,
    canonical_unit). A SNAPSHOT, never a flow: each row is "the known sellable
    supply of this producer at the end of this day" — never a quantity added
    that day, never additive across days. Generated once per day, for TODAY
    only (never backfilled for a past day — no snapshot exists before this
    job first runs)."""

    __tablename__ = "producer_supply_daily_snapshot"
    __table_args__ = (
        Index(
            "producer_supply_daily_snapshot_grain_uq", "metric_date", "producer_id", "zone_id",
            "category_id", "sub_category_id", "canonical_unit", unique=True,
        ),
        Index("producer_supply_daily_snapshot_date_idx", "metric_date"),
        CheckConstraint("measurement_family IN ('MASS','VOLUME','COUNT','PACKAGE','OTHER')", name="producer_supply_daily_snapshot_family_chk"),
        CheckConstraint("available_quantity >= 0 AND product_count >= 0", name="producer_supply_daily_snapshot_qty_chk"),
        {"schema": "analytics"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    metric_date = Column(Date, nullable=False)
    producer_id = Column(PG_UUID(as_uuid=True), nullable=False)
    zone_id = Column(PG_UUID(as_uuid=True), nullable=False, server_default=text("'00000000-0000-0000-0000-000000000000'"))
    category_id = Column(PG_UUID(as_uuid=True), nullable=False, server_default=text("'00000000-0000-0000-0000-000000000000'"))
    sub_category_id = Column(PG_UUID(as_uuid=True), nullable=False, server_default=text("'00000000-0000-0000-0000-000000000000'"))
    canonical_unit = Column(Text, nullable=False)
    measurement_family = Column(Text, nullable=False)
    available_quantity = Column(Numeric(16, 3), nullable=False, server_default=text("'0'"))
    product_count = Column(Integer, nullable=False, server_default=text("0"))
    computed_at = Column(_tz(), server_default=func.now(), nullable=False)


__all__ = [
    "EventOutboxRecord",
    "BusinessEventRecord",
    "MetricTargetRecord",
    "BuyerDailyMetricRecord",
    "DirectDailyMetricRecord",
    "TenderDailyMetricRecord",
    "RecurringDailyMetricRecord",
    "ProducerDailyMetricRecord",
    "ProducerQuantityDailyMetricRecord",
    "ProducerSupplyDailySnapshotRecord",
]
