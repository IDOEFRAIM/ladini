"""Idempotent refresh of the Producer Metric Layer (Phase C) daily-metrics tables.

Mirrors `daily_metrics_refresh.py` exactly in pattern (advisory lock, DELETE+INSERT
per day, pure aggregators, cohort-by-creation-day semantics) — see that module's
docstring for the general discipline, not repeated here.

Producer identity: `producer_id` is resolved from the SAME persisted relationships
the emitter uses (`domain/analytics/emitter.py::_resolve_direct_producer_id` /
`_resolve_tender_producer_id`) — Order->OrderItem->Product.producer_id for
DIRECT/RECURRING, Order->Bid.producer_id for TENDER — expressed here as SQL joins
instead of an ORM walk, for the same reason the buyer refresher queries
transactional tables directly rather than `business_events`: full historical
reconstructibility, not just facts landed after Phase B's event enrichment.

`producer_daily_metrics`/`producer_quantity_daily_metrics` are FLOWS (cohort +
current state, recomputable for any past day). `producer_supply_daily_snapshot`
is a SNAPSHOT of the CURRENT `marketplace.products` state — see
`snapshot_producer_supply` below, which deliberately does NOT share
`recompute_day`'s "any past day" contract.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from ladini.domain.analytics.daily_aggregation import (
    NIL_UUID,
    resolve_canonical,
    to_canonical_quantity,
)
from ladini.domain.analytics.models import (
    ProducerDailyMetricRecord,
    ProducerQuantityDailyMetricRecord,
    ProducerSupplyDailySnapshotRecord,
)
from ladini.domain.analytics.producer_daily_aggregation import (
    ACTIVITY_EVENT_NAMES,
    ProducerActivityEventFact,
    ProducerOrderFact,
    ProducerQuantityItemFact,
    aggregate_producer_daily,
    aggregate_producer_quantity_daily,
)
from ladini.domain.analytics.units import measurement_family_of
from ladini.services.analytics.daily_metrics_refresh import (
    DIRECT_COHORT_TS,
    DIRECT_ORDER_WHERE,
)

logger = logging.getLogger("Ladini.Analytics.ProducerMetricsRefresh")

_UUID_COLUMNS = ("producer_id", "zone_id", "category_id", "sub_category_id")

# DIRECT: one order = one producer (cart splitting, see PRODUCER_ANALYTICS_ARCHITECTURE.md
# §17) — any one item's product.producer_id identifies the whole order.
_DIRECT_ORDERS_SQL = text(
    f"""
    SELECT o.id AS order_id, p.producer_id AS producer_id, pr.zone_id AS zone_id,
           o.total_amount, o.delivery_status,
           (o.status IN ('CONFIRMED', 'COMPLETED') OR EXISTS (
               SELECT 1 FROM analytics.business_events be
               WHERE be.event_name = 'DIRECT_ORDER_CONFIRMED' AND be.entity_type = 'ORDER' AND be.entity_id = o.id
           )) AS confirmed
    FROM marketplace.orders o
    JOIN LATERAL (SELECT oi.product_id FROM marketplace.order_items oi WHERE oi.order_id = o.id LIMIT 1) x ON true
    JOIN marketplace.products p ON p.id = x.product_id
    LEFT JOIN marketplace.producers pr ON pr.id = p.producer_id
    WHERE {DIRECT_COHORT_TS} >= :start AND {DIRECT_COHORT_TS} < :end AND {DIRECT_ORDER_WHERE}
    ORDER BY o.id
    """
)

_DIRECT_ITEMS_SQL = text(
    f"""
    SELECT p.producer_id AS producer_id, p.unit, sc.priority_unit, oi.quantity, o.delivery_status,
           (o.status IN ('CONFIRMED', 'COMPLETED') OR EXISTS (
               SELECT 1 FROM analytics.business_events be
               WHERE be.event_name = 'DIRECT_ORDER_CONFIRMED' AND be.entity_type = 'ORDER' AND be.entity_id = o.id
           )) AS confirmed
    FROM marketplace.orders o
    JOIN marketplace.order_items oi ON oi.order_id = o.id
    JOIN marketplace.products p ON p.id = oi.product_id
    LEFT JOIN governance.sub_categories sc ON sc.id = p.sub_category_id
    WHERE {DIRECT_COHORT_TS} >= :start AND {DIRECT_COHORT_TS} < :end AND {DIRECT_ORDER_WHERE}
    ORDER BY o.id
    """
)

# TENDER: producer = the winning bidder (Order.winning_bid_id -> Bid.producer_id). No
# OrderItem exists for a TENDER order at all (Phase A §3.2 finding I) -> no quantity facts.
# The order's own creation (select_winning_bid) IS the commitment; no separate CONFIRMED step.
_TENDER_ORDERS_SQL = text(
    """
    SELECT o.id AS order_id, b.producer_id AS producer_id, pr.zone_id AS zone_id,
           o.total_amount, o.delivery_status
    FROM marketplace.orders o
    JOIN marketplace.bids b ON b.id = o.winning_bid_id
    LEFT JOIN marketplace.producers pr ON pr.id = b.producer_id
    WHERE o.auction_id IS NOT NULL AND o.created_at >= :start AND o.created_at < :end
    ORDER BY o.id
    """
)

# RECURRING: one order per producer per occurrence-acceptance (accept_match_proposal) ->
# same "any one item identifies the order" reasoning as DIRECT. "Delivered" = RECEIVED
# (buyer-confirmed), same rule the buyer layer already uses for this journey.
_RECURRING_ORDERS_SQL = text(
    """
    SELECT o.id AS order_id, p.producer_id AS producer_id, pr.zone_id AS zone_id,
           o.total_amount, o.delivery_status
    FROM marketplace.orders o
    JOIN LATERAL (SELECT oi.product_id FROM marketplace.order_items oi WHERE oi.order_id = o.id LIMIT 1) x ON true
    JOIN marketplace.products p ON p.id = x.product_id
    LEFT JOIN marketplace.producers pr ON pr.id = p.producer_id
    WHERE o.order_type = 'RECURRING_SUPPLY' AND o.created_at >= :start AND o.created_at < :end
    ORDER BY o.id
    """
)

_RECURRING_ITEMS_SQL = text(
    """
    SELECT p.producer_id AS producer_id, p.unit, sc.priority_unit, oi.quantity, o.delivery_status
    FROM marketplace.orders o
    JOIN marketplace.order_items oi ON oi.order_id = o.id
    JOIN marketplace.products p ON p.id = oi.product_id
    LEFT JOIN governance.sub_categories sc ON sc.id = p.sub_category_id
    WHERE o.order_type = 'RECURRING_SUPPLY' AND o.created_at >= :start AND o.created_at < :end
    ORDER BY o.id
    """
)

# Activity-only signals: facts with no corresponding order row (publishing, a quantity
# change outside a sale, a bid that has not [yet, or never] won). Every one of these events
# already carries producer_id (Producer Analytics Phase B).
_ACTIVITY_EVENTS_SQL = text(
    """
    SELECT be.event_name, be.producer_id, pr.zone_id AS zone_id
    FROM analytics.business_events be
    LEFT JOIN marketplace.producers pr ON pr.id = be.producer_id
    WHERE be.producer_id IS NOT NULL AND be.event_name = ANY(:names)
      AND be.occurred_at >= :start_tz AND be.occurred_at < :end_tz
    ORDER BY be.id
    """
)


def _as_uuid(value: Any) -> Any:
    import uuid

    return uuid.UUID(str(value)) if value is not None else None


class ProducerMetricsRefresher:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def recompute_day(self, day: date) -> dict[str, int]:
        """Rebuild `producer_daily_metrics` + `producer_quantity_daily_metrics` for UTC
        day `day` (FLOWS — any past day). Does NOT include the supply snapshot (see
        `snapshot_producer_supply`). Does NOT commit (the caller owns the transaction)."""
        today = datetime.now(timezone.utc).date()
        if day > today:
            raise ValueError(f"Cannot compute producer daily metrics for a future day ({day} > {today}).")

        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"producer_daily_metrics:{day.isoformat()}"}
        )
        start = datetime(day.year, day.month, day.day)
        end = start + timedelta(days=1)
        params = {
            "start": start, "end": end,
            "start_tz": start.replace(tzinfo=timezone.utc), "end_tz": end.replace(tzinfo=timezone.utc),
        }

        direct_orders = (await self.session.execute(_DIRECT_ORDERS_SQL, params)).mappings().all()
        tender_orders = (await self.session.execute(_TENDER_ORDERS_SQL, params)).mappings().all()
        recurring_orders = (await self.session.execute(_RECURRING_ORDERS_SQL, params)).mappings().all()
        direct_items = (await self.session.execute(_DIRECT_ITEMS_SQL, params)).mappings().all()
        recurring_items = (await self.session.execute(_RECURRING_ITEMS_SQL, params)).mappings().all()
        activity_rows = (
            await self.session.execute(_ACTIVITY_EVENTS_SQL, {**params, "names": list(ACTIVITY_EVENT_NAMES)})
        ).mappings().all()

        orders = (
            [ProducerOrderFact(journey="DIRECT", **dict(r)) for r in direct_orders]
            + [ProducerOrderFact(journey="TENDER", **dict(r)) for r in tender_orders]
            + [ProducerOrderFact(journey="RECURRING", **dict(r)) for r in recurring_orders]
        )
        items = (
            [ProducerQuantityItemFact(journey="DIRECT", **dict(r)) for r in direct_items]
            + [ProducerQuantityItemFact(journey="RECURRING", **dict(r)) for r in recurring_items]
        )
        activity = [ProducerActivityEventFact(**dict(r)) for r in activity_rows]

        plan = (
            (ProducerDailyMetricRecord, aggregate_producer_daily(day, orders, activity)),
            (ProducerQuantityDailyMetricRecord, aggregate_producer_quantity_daily(day, items)),
        )
        written: dict[str, int] = {}
        for model, rows in plan:
            await self.session.execute(delete(model).where(model.metric_date == day))
            if rows:
                await self.session.execute(insert(model), [self._bind(r) for r in rows])
            written[model.__tablename__] = len(rows)
        logger.info("producer_daily_metrics.recomputed | day=%s | rows=%s", day, written)
        return written

    @staticmethod
    def _bind(row: dict[str, Any]) -> dict[str, Any]:
        return {k: (_as_uuid(v) if k in _UUID_COLUMNS else v) for k, v in row.items()}


_SNAPSHOT_SQL = text(
    """
    SELECT p.producer_id, pr.zone_id, sc.category_id, p.sub_category_id, p.unit, sc.priority_unit,
           sum(p.quantity_for_sale) AS quantity, count(*) AS product_count
    FROM marketplace.products p
    LEFT JOIN marketplace.producers pr ON pr.id = p.producer_id
    LEFT JOIN governance.sub_categories sc ON sc.id = p.sub_category_id
    WHERE p.is_available = TRUE AND p.quantity_for_sale > 0
    GROUP BY p.producer_id, pr.zone_id, sc.category_id, p.sub_category_id, p.unit, sc.priority_unit
    """
)


async def snapshot_producer_supply(session: AsyncSession, day: date) -> int:
    """Write TODAY's end-of-day `producer_supply_daily_snapshot` rows — a SNAPSHOT of
    `marketplace.products`' CURRENT state, never a flow, never additive across days.

    Deliberately refuses any `day` other than today (UTC): unlike `recompute_day`, a
    past day's snapshot is NOT reconstructible (no historical ledger of `is_available`/
    `quantity_for_sale` existed before Producer Analytics Phase B's
    `PRODUCT_SELLABLE_QUANTITY_CHANGED` event, and even that only started accumulating
    forward from Phase B, not a point-in-time gauge) — fabricating one from today's
    state would misrepresent history. Idempotent for today: re-running it replaces
    today's rows with the same live query, safe to call more than once."""
    today = datetime.now(timezone.utc).date()
    if day != today:
        raise ValueError(
            f"producer_supply_daily_snapshot is a live snapshot, not reconstructible for a past day "
            f"(asked for {day}, today is {today})."
        )
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"producer_supply_daily_snapshot:{day.isoformat()}"}
    )
    rows = (await session.execute(_SNAPSHOT_SQL)).mappings().all()
    grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for r in rows:
        if not r["producer_id"]:
            continue
        canonical = resolve_canonical(r["unit"], r["priority_unit"])
        key = (
            str(r["producer_id"]),
            str(r["zone_id"]) if r["zone_id"] else NIL_UUID,
            str(r["category_id"]) if r["category_id"] else NIL_UUID,
            str(r["sub_category_id"]) if r["sub_category_id"] else NIL_UUID,
            canonical,
        )
        g = grouped.setdefault(key, {
            "metric_date": day, "producer_id": key[0], "zone_id": key[1], "category_id": key[2],
            "sub_category_id": key[3], "canonical_unit": canonical, "measurement_family": measurement_family_of(canonical),
            "available_quantity": 0, "product_count": 0,
        })
        converted = to_canonical_quantity(r["quantity"] or 0, r["unit"], canonical)
        g["available_quantity"] = float(g["available_quantity"]) + float(converted)
        g["product_count"] += int(r["product_count"])

    await session.execute(delete(ProducerSupplyDailySnapshotRecord).where(ProducerSupplyDailySnapshotRecord.metric_date == day))
    out_rows = [ProducerMetricsRefresher._bind(g) for g in grouped.values()]
    if out_rows:
        await session.execute(insert(ProducerSupplyDailySnapshotRecord), out_rows)
    logger.info("producer_supply_daily_snapshot.recomputed | day=%s | rows=%s", day, len(out_rows))
    return len(out_rows)


async def recompute_range(session_factory: Any, start: date, end: date) -> dict[str, dict[str, int]]:
    """Recompute the two FLOW tables for every day in [start, end] (inclusive), one
    transaction per day. `session_factory` is an async context manager yielding a
    committing session (e.g. `workers.runtime.worker_session`)."""
    out: dict[str, dict[str, int]] = {}
    day = start
    while day <= end:
        async with session_factory() as session:
            out[day.isoformat()] = await ProducerMetricsRefresher(session).recompute_day(day)
        day += timedelta(days=1)
    return out


async def recompute_recent(session_factory: Any, lookback_days: int = 14, *, today: Any = None) -> dict[str, dict[str, int]]:
    today = today or datetime.now(timezone.utc).date()
    out = await recompute_range(session_factory, today - timedelta(days=lookback_days), today)
    async with session_factory() as session:
        out[f"{today.isoformat()}:supply_snapshot"] = {"producer_supply_daily_snapshot": await snapshot_producer_supply(session, today)}
    return out


__all__ = [
    "ProducerMetricsRefresher", "snapshot_producer_supply", "recompute_range", "recompute_recent",
]
