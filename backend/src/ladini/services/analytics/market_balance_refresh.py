"""Market Balance (Phase E) daily snapshot refresh.

Fetches the current RECURRING/TENDER demand facts and the latest producer
supply snapshot, hands them to the pure `aggregate_market_balance`, and
persists the result — a SNAPSHOT, never a flow, same discipline as
`producer_metrics_refresh.py::snapshot_producer_supply`: refuses any day but
today, idempotent DELETE+INSERT under an advisory lock. See
`docs/analytics/MARKET_BALANCE.md` for the semantic audit this implements.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from ladini.domain.analytics.market_balance_aggregation import (
    RecurringDemandFact,
    SupplyFact,
    TenderDemandFact,
    aggregate_market_balance,
)
from ladini.domain.analytics.models import MarketBalanceDailySnapshotRecord

logger = logging.getLogger("Ladini.Analytics.MarketBalanceRefresh")

_UUID_COLUMNS = ("zone_scope", "category_id", "sub_category_id")

#: Operational-recency window for RECURRING occurrences (docs/analytics/MARKET_BALANCE.md §7) —
#: PROVISIONAL, not calibrated against real cadence data. Configurable, not hardcoded silently.
DEFAULT_LOOKBACK_DAYS = 3
DEFAULT_LOOKAHEAD_DAYS = 7


def _as_uuid(value: Any) -> Any:
    return uuid.UUID(str(value)) if value is not None else None


def _bind(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (_as_uuid(v) if k in _UUID_COLUMNS else v) for k, v in row.items()}


def lookback_days() -> int:
    return int(os.getenv("MARKET_BALANCE_LOOKBACK_DAYS", str(DEFAULT_LOOKBACK_DAYS)))


def lookahead_days() -> int:
    return int(os.getenv("MARKET_BALANCE_LOOKAHEAD_DAYS", str(DEFAULT_LOOKAHEAD_DAYS)))


_RECURRING_DEMAND_SQL = text(
    """
    SELECT u.zone_id AS zone_id, sc.category_id AS category_id, rn.sub_category_id AS sub_category_id,
           o.unit, sc.priority_unit, (o.requested_quantity - o.quantity_confirmed) AS open_quantity
    FROM marketplace.recurring_need_occurrences o
    JOIN marketplace.recurring_needs rn ON rn.id = o.recurring_need_id
    JOIN marketplace.buyer_profiles bp ON bp.id = rn.buyer_id
    JOIN auth.users u ON u.id = bp.user_id
    LEFT JOIN governance.sub_categories sc ON sc.id = rn.sub_category_id
    WHERE o.status IN ('OPEN', 'MATCHED')
      AND o.occurrence_date >= :lookback AND o.occurrence_date < :lookahead
      AND (o.requested_quantity - o.quantity_confirmed) > 0
    """
)

_TENDER_DEMAND_SQL = text(
    """
    SELECT a.target_zone_id AS zone_id, sc.category_id AS category_id, a.sub_category_id AS sub_category_id,
           a.unit, sc.priority_unit, a.quantity
    FROM marketplace.auctions a
    LEFT JOIN governance.sub_categories sc ON sc.id = a.sub_category_id
    WHERE a.status = 'OPEN' AND a.deadline > :now
    """
)

_SUPPLY_SQL = text(
    """
    SELECT zone_id, category_id, sub_category_id, canonical_unit, SUM(available_quantity) AS available_quantity
    FROM analytics.producer_supply_daily_snapshot
    WHERE metric_date = :latest_day
    GROUP BY zone_id, category_id, sub_category_id, canonical_unit
    """
)


async def recompute_today(session: AsyncSession, *, today: date | None = None) -> int:
    """Rebuilds TODAY's `market_balance_daily_snapshot` rows. Refuses any other day — like
    `snapshot_producer_supply`, this is a live snapshot of "still open now"/"available now", not
    reconstructible for a past day (docs/analytics/MARKET_BALANCE.md §16/§18)."""
    now = datetime.now(timezone.utc)
    real_today = now.date()
    if today is not None and today != real_today:
        raise ValueError(f"market_balance_daily_snapshot is a live snapshot, not reconstructible for a past day (asked for {today}, today is {real_today}).")
    snapshot_day = real_today

    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"market_balance_daily_snapshot:{snapshot_day.isoformat()}"})

    # `occurrence_date`/`deadline` are naive `DateTime` columns (implicitly UTC, same convention as
    # `data_quality.py`'s own recurring-occurrence checks) — naive bind parameters throughout, never
    # tz-aware ones, to avoid an asyncpg naive/aware comparison mismatch.
    now_naive = now.replace(tzinfo=None)
    today_naive = datetime(snapshot_day.year, snapshot_day.month, snapshot_day.day)
    lookback = today_naive - timedelta(days=lookback_days())
    lookahead = today_naive + timedelta(days=lookahead_days() + 1)
    recurring_rows = (await session.execute(_RECURRING_DEMAND_SQL, {"lookback": lookback, "lookahead": lookahead})).mappings().all()
    tender_rows = (await session.execute(_TENDER_DEMAND_SQL, {"now": now_naive})).mappings().all()
    latest_supply_day = (await session.execute(text("SELECT max(metric_date) FROM analytics.producer_supply_daily_snapshot"))).scalar()
    supply_rows = (
        (await session.execute(_SUPPLY_SQL, {"latest_day": latest_supply_day})).mappings().all()
        if latest_supply_day is not None else []
    )
    if latest_supply_day is None:
        logger.warning("market_balance.no_supply_snapshot | snapshot_day=%s", snapshot_day)

    recurring_facts = [
        RecurringDemandFact(zone_id=r["zone_id"], category_id=r["category_id"], sub_category_id=r["sub_category_id"],
                             unit=r["unit"], priority_unit=r["priority_unit"], open_quantity=r["open_quantity"])
        for r in recurring_rows
    ]
    tender_facts = [
        TenderDemandFact(zone_id=r["zone_id"], category_id=r["category_id"], sub_category_id=r["sub_category_id"],
                          unit=r["unit"], priority_unit=r["priority_unit"], quantity=r["quantity"])
        for r in tender_rows
    ]
    supply_facts = [
        SupplyFact(zone_id=r["zone_id"], category_id=r["category_id"], sub_category_id=r["sub_category_id"],
                    canonical_unit=r["canonical_unit"], available_quantity=r["available_quantity"])
        for r in supply_rows
    ]

    rows = aggregate_market_balance(snapshot_day, recurring_facts, tender_facts, supply_facts)
    await session.execute(delete(MarketBalanceDailySnapshotRecord).where(MarketBalanceDailySnapshotRecord.snapshot_day == snapshot_day))
    if rows:
        await session.execute(insert(MarketBalanceDailySnapshotRecord), [_bind(r) for r in rows])
    logger.info("market_balance.recomputed | snapshot_day=%s | rows=%s", snapshot_day, len(rows))
    return len(rows)


__all__ = ["recompute_today", "lookback_days", "lookahead_days", "DEFAULT_LOOKBACK_DAYS", "DEFAULT_LOOKAHEAD_DAYS"]
