"""Idempotent refresh of the Phase D daily-metrics tables.

`recompute_day(D)` rebuilds ALL FOUR tables for UTC day D from the source of truth
(transactional tables for the state of the business, `business_events` for
searches/digests — the only source of those facts) and replaces the day's rows
(`DELETE` + `INSERT` in one transaction, guarded by a per-day advisory lock so two
concurrent refreshes of the same day serialize). Replaying it yields the same rows;
recomputing an arbitrary historical day is the same call (`recompute_day(date(...))`).

Late-arriving facts: rows are COHORTS (needs created on D + their CURRENT state), so a
delivery recorded today for an order created last week changes last week's row the
next time that day is recomputed. `recompute_recent(lookback_days)` — what the nightly
cron runs — recomputes today and the previous N days; older corrections are a targeted
`recompute_day`/`recompute_range` call.

Raw cohort facts are fetched with light per-day queries and aggregated by the pure
functions in `domain/analytics/daily_aggregation.py` (unit-testable without a database).
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import delete, insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from ladini.domain.analytics.daily_aggregation import (
    DigestFact,
    DirectOrderFact,
    RecurringFact,
    SearchFact,
    TenderFact,
    aggregate_buyer,
    aggregate_direct,
    aggregate_recurring,
    aggregate_tender,
)
from ladini.domain.analytics.models import (
    BuyerDailyMetricRecord,
    DirectDailyMetricRecord,
    RecurringDailyMetricRecord,
    TenderDailyMetricRecord,
)

logger = logging.getLogger("Ladini.Analytics.DailyMetricsRefresh")

_UUID_COLUMNS = ("buyer_id", "zone_id", "category_id", "sub_category_id")

_DIRECT_ORDERS_SQL = text(
    """
    SELECT o.buyer_id, COALESCE(o.zone_id, u.zone_id) AS zone_id, sc.category_id AS category_id,
           sc.id AS sub_category_id, o.total_amount, o.status, o.delivery_status
    FROM marketplace.orders o
    LEFT JOIN marketplace.buyer_profiles bp ON bp.id = o.buyer_id
    LEFT JOIN auth.users u ON u.id = bp.user_id
    LEFT JOIN LATERAL (
        SELECT CASE WHEN count(DISTINCT p.sub_category_id) = 1 AND count(*) = count(p.sub_category_id)
                    THEN min(p.sub_category_id::text)::uuid END AS sub_category_id
        FROM marketplace.order_items oi
        JOIN marketplace.products p ON p.id = oi.product_id
        WHERE oi.order_id = o.id
    ) x ON true
    LEFT JOIN governance.sub_categories sc ON sc.id = x.sub_category_id
    WHERE o.created_at >= :start AND o.created_at < :end
      AND o.auction_id IS NULL AND o.order_type = 'STANDARD' AND o.buyer_id IS NOT NULL
    ORDER BY o.id
    """
)

_SEARCH_EVENTS_SQL = text(
    """
    SELECT event_name, zone_id FROM analytics.business_events
    WHERE journey = 'DIRECT' AND event_name IN ('DIRECT_SEARCH_PERFORMED', 'DIRECT_SEARCH_SUCCEEDED')
      AND occurred_at >= :start_tz AND occurred_at < :end_tz
    ORDER BY id
    """
)

_TENDERS_SQL = text(
    """
    SELECT a.buyer_id, COALESCE(a.target_zone_id, u.zone_id) AS zone_id, sc.category_id AS category_id,
           a.sub_category_id, a.quantity, a.max_price_per_unit,
           (SELECT count(*) FROM marketplace.bids b WHERE b.auction_id = a.id) AS bids_count,
           (SELECT EXTRACT(EPOCH FROM (min(b.created_at) - a.created_at))
              FROM marketplace.bids b WHERE b.auction_id = a.id) AS first_bid_latency_seconds,
           (a.winner_bid_id IS NOT NULL) AS has_winner,
           (SELECT b.offered_price FROM marketplace.bids b WHERE b.id = a.winner_bid_id) AS winner_price,
           (SELECT count(*) FROM marketplace.orders o WHERE o.auction_id = a.id) AS orders_count,
           (SELECT count(*) FROM marketplace.orders o
             WHERE o.auction_id = a.id AND o.delivery_status IN ('DELIVERED', 'FULFILLED')) AS delivered_orders_count
    FROM marketplace.auctions a
    LEFT JOIN marketplace.buyer_profiles bp ON bp.id = a.buyer_id
    LEFT JOIN auth.users u ON u.id = bp.user_id
    LEFT JOIN governance.sub_categories sc ON sc.id = a.sub_category_id
    WHERE a.created_at >= :start AND a.created_at < :end
    ORDER BY a.id
    """
)

# `oc.order_group_id` <-> `orders.checkout_group_id`: both are written in the SAME transaction by
# `accept_match_proposal`, so the join is exact for every accepted occurrence (no FK, but no gap).
_RECURRING_SQL = text(
    """
    SELECT oc.id AS occurrence_id, n.id AS need_id, n.buyer_id, u.zone_id, sc.category_id AS category_id,
           n.sub_category_id, sc.priority_unit, oc.unit, oc.requested_quantity, oc.quantity_matched,
           oc.quantity_confirmed, oc.status, (oc.notified_at IS NOT NULL) AS notified,
           COALESCE((SELECT sum(na.quantity * na.unit_price) FROM marketplace.need_allocations na
                      WHERE na.occurrence_id = oc.id AND na.status IN ('PROPOSED', 'ACCEPTED', 'CONVERTED')), 0) AS potential_value,
           COALESCE((SELECT sum(na.quantity * na.unit_price) FROM marketplace.need_allocations na
                      WHERE na.occurrence_id = oc.id AND na.status = 'CONVERTED'), 0) AS confirmed_value,
           (SELECT count(*) FROM marketplace.orders o
             WHERE oc.order_group_id IS NOT NULL AND o.checkout_group_id = oc.order_group_id
               AND o.order_type = 'RECURRING_SUPPLY') AS orders_count,
           (SELECT count(*) FROM marketplace.orders o
             WHERE oc.order_group_id IS NOT NULL AND o.checkout_group_id = oc.order_group_id
               AND o.order_type = 'RECURRING_SUPPLY' AND o.delivery_status = 'RECEIVED') AS received_orders_count,
           COALESCE((SELECT sum(o.total_amount) FROM marketplace.orders o
             WHERE oc.order_group_id IS NOT NULL AND o.checkout_group_id = oc.order_group_id
               AND o.order_type = 'RECURRING_SUPPLY' AND o.delivery_status = 'RECEIVED'), 0) AS received_value
    FROM marketplace.recurring_need_occurrences oc
    JOIN marketplace.recurring_needs n ON n.id = oc.recurring_need_id
    JOIN marketplace.buyer_profiles bp ON bp.id = n.buyer_id
    JOIN auth.users u ON u.id = bp.user_id
    JOIN governance.sub_categories sc ON sc.id = n.sub_category_id
    WHERE oc.occurrence_date >= :start AND oc.occurrence_date < :end
    ORDER BY oc.id
    """
)

_DIGEST_EVENTS_SQL = text(
    """
    SELECT be.event_name, be.buyer_id, u.zone_id
    FROM analytics.business_events be
    LEFT JOIN marketplace.buyer_profiles bp ON bp.id = be.buyer_id
    LEFT JOIN auth.users u ON u.id = bp.user_id
    WHERE be.event_name IN ('RECURRING_DIGEST_SENT', 'RECURRING_DIGEST_ACCEPTED')
      AND be.occurred_at >= :start_tz AND be.occurred_at < :end_tz
    ORDER BY be.id
    """
)


def _as_uuid(value: Any) -> Any:
    return uuid.UUID(str(value)) if value is not None else None


class DailyMetricsRefresher:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def recompute_day(self, day: date) -> dict[str, int]:
        """Rebuild the four tables for UTC day `day`. Does NOT commit (the caller owns the
        transaction) so the DELETE+INSERT of the day is atomic."""
        today = datetime.now(timezone.utc).date()
        if day > today:
            raise ValueError(f"Cannot compute daily metrics for a future day ({day} > {today}).")

        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"analytics_daily_metrics:{day.isoformat()}"}
        )
        start = datetime(day.year, day.month, day.day)
        end = start + timedelta(days=1)
        params = {
            "start": start, "end": end,
            "start_tz": start.replace(tzinfo=timezone.utc), "end_tz": end.replace(tzinfo=timezone.utc),
        }

        orders = [DirectOrderFact(**dict(r)) for r in (await self.session.execute(_DIRECT_ORDERS_SQL, params)).mappings().all()]
        searches = [
            SearchFact(zone_id=r["zone_id"], succeeded=r["event_name"] == "DIRECT_SEARCH_SUCCEEDED")
            for r in (await self.session.execute(_SEARCH_EVENTS_SQL, params)).mappings().all()
        ]
        tenders = [TenderFact(**dict(r)) for r in (await self.session.execute(_TENDERS_SQL, params)).mappings().all()]
        recurring = [RecurringFact(**dict(r)) for r in (await self.session.execute(_RECURRING_SQL, params)).mappings().all()]
        digests = [
            DigestFact(buyer_id=r["buyer_id"], zone_id=r["zone_id"], accepted=r["event_name"] == "RECURRING_DIGEST_ACCEPTED")
            for r in (await self.session.execute(_DIGEST_EVENTS_SQL, params)).mappings().all()
        ]

        plan = (
            (DirectDailyMetricRecord, aggregate_direct(day, orders, searches)),
            (TenderDailyMetricRecord, aggregate_tender(day, tenders)),
            (RecurringDailyMetricRecord, aggregate_recurring(day, recurring)),
            (BuyerDailyMetricRecord, aggregate_buyer(day, orders, tenders, recurring, digests)),
        )
        written: dict[str, int] = {}
        for model, rows in plan:
            await self.session.execute(delete(model).where(model.metric_date == day))
            if rows:
                await self.session.execute(insert(model), [self._bind(r) for r in rows])
            written[model.__tablename__] = len(rows)
        logger.info("daily_metrics.recomputed | day=%s | rows=%s", day, written)
        return written

    @staticmethod
    def _bind(row: dict[str, Any]) -> dict[str, Any]:
        return {k: (_as_uuid(v) if k in _UUID_COLUMNS else v) for k, v in row.items()}


async def recompute_range(session_factory: Any, start: date, end: date) -> dict[str, dict[str, int]]:
    """Recompute every day in [start, end] (inclusive), one transaction per day so one bad day never
    rolls back the others. `session_factory` is an async context manager yielding a committing
    session (e.g. `workers.runtime.worker_session`)."""
    out: dict[str, dict[str, int]] = {}
    day = start
    while day <= end:
        async with session_factory() as session:
            out[day.isoformat()] = await DailyMetricsRefresher(session).recompute_day(day)
        day += timedelta(days=1)
    return out


async def recompute_recent(session_factory: Any, lookback_days: int = 14, *, today: Optional[date] = None) -> dict[str, dict[str, int]]:
    today = today or datetime.now(timezone.utc).date()
    return await recompute_range(session_factory, today - timedelta(days=lookback_days), today)


__all__ = ["DailyMetricsRefresher", "recompute_range", "recompute_recent"]
