"""Data-quality checks over the Phase D daily aggregates (no UI — returns plain issues).

Negative values are prevented by CHECK constraints on the tables themselves; the checks
here cover what a constraint cannot express: physical consistency, unit sanity, coverage
of the refresh, staleness and grain uniqueness.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

STALE_AFTER = timedelta(hours=36)
_EPS = 0.0005


@dataclass(frozen=True)
class QualityIssue:
    check: str
    table: str
    severity: str  # "ERROR" | "WARNING"
    count: int
    detail: str


_GRAINS = {
    "analytics.buyer_daily_metrics": "metric_date, buyer_id",
    "analytics.direct_daily_metrics": "metric_date, zone_id, category_id, sub_category_id",
    "analytics.tender_daily_metrics": "metric_date, zone_id, category_id, sub_category_id",
    "analytics.recurring_daily_metrics": "metric_date, zone_id, category_id, sub_category_id, canonical_unit",
}

# Days with source facts but no aggregate row (a refresh that never ran / failed for that day).
_MISSING_DAYS = {
    "analytics.direct_daily_metrics": (
        "SELECT DISTINCT o.created_at::date AS d FROM marketplace.orders o WHERE o.auction_id IS NULL AND o.order_type = 'STANDARD' "
        "AND o.buyer_id IS NOT NULL AND o.status NOT IN ('DRAFT', 'SUPERSEDED') AND o.created_at >= :s AND o.created_at < :e"
    ),
    "analytics.tender_daily_metrics": "SELECT DISTINCT a.created_at::date AS d FROM marketplace.auctions a WHERE a.created_at >= :s AND a.created_at < :e",
    "analytics.recurring_daily_metrics": (
        "SELECT DISTINCT oc.occurrence_date::date AS d FROM marketplace.recurring_need_occurrences oc "
        "WHERE oc.occurrence_date >= :s AND oc.occurrence_date < :e AND oc.occurrence_date < :today_end"
    ),
}


async def run_data_quality_checks(
    session: AsyncSession, start: date, end: date, *, now: Optional[datetime] = None,
) -> list[QualityIssue]:
    now = now or datetime.now(timezone.utc)
    today = now.date()
    s_dt = datetime(start.year, start.month, start.day)
    e_dt = datetime(end.year, end.month, end.day) + timedelta(days=1)
    issues: list[QualityIssue] = []
    rec = "analytics.recurring_daily_metrics"
    win = "metric_date >= :s AND metric_date <= :e"
    p = {"s": start, "e": end}

    async def scalar(sql: str, params: dict) -> int:
        return int((await session.execute(text(sql), params)).scalar() or 0)

    n = await scalar(f"SELECT count(*) FROM {rec} WHERE {win} AND matched_quantity > requested_quantity + {_EPS}", p)
    if n:
        issues.append(QualityIssue("matched_gt_requested", rec, "ERROR", n, "matched_quantity exceeds requested_quantity."))
    n = await scalar(f"SELECT count(*) FROM {rec} WHERE {win} AND confirmed_quantity > matched_quantity + {_EPS}", p)
    if n:
        issues.append(QualityIssue("confirmed_gt_matched", rec, "WARNING", n, "confirmed_quantity exceeds matched_quantity."))

    rows = (await session.execute(text(f"SELECT DISTINCT canonical_unit FROM {rec} WHERE {win} AND measurement_family = 'OTHER'"), p)).all()
    if rows:
        issues.append(QualityIssue("unknown_canonical_unit", rec, "WARNING", len(rows),
                                   "Units outside the registry (cannot be aggregated): " + ", ".join(sorted(str(r[0]) for r in rows))))
    n = await scalar(
        f"SELECT count(*) FROM (SELECT sub_category_id FROM {rec} WHERE {win} AND sub_category_id <> '00000000-0000-0000-0000-000000000000' "
        "GROUP BY sub_category_id HAVING count(DISTINCT measurement_family) > 1) t", p)
    if n:
        issues.append(QualityIssue("incompatible_aggregation", rec, "ERROR", n,
                                   "A sub-category appears with several measurement families in the window (units cannot be combined)."))

    for table, sql in _MISSING_DAYS.items():
        params = {"s": s_dt, "e": e_dt, "today_end": datetime(today.year, today.month, today.day) + timedelta(days=1)}
        query = f"SELECT count(*) FROM ({sql}) x WHERE NOT EXISTS (SELECT 1 FROM {table} m WHERE m.metric_date = x.d)"
        n = await scalar(query, {k: v for k, v in params.items() if f":{k}" in sql or f":{k}" in query})
        if n:
            issues.append(QualityIssue("missing_aggregate_days", table, "ERROR", n, "Days with source facts but no aggregate row: recompute them."))

    for table, grain in _GRAINS.items():
        n = await scalar(f"SELECT count(*) FROM (SELECT 1 FROM {table} GROUP BY {grain} HAVING count(*) > 1) t", {})
        if n:
            issues.append(QualityIssue("duplicate_grain", table, "ERROR", n, f"More than one row for the same grain ({grain})."))
        last = (await session.execute(text(f"SELECT max(computed_at) FROM {table}"))).scalar()
        if last is None or now - last > STALE_AFTER:
            issues.append(QualityIssue("stale_refresh", table, "WARNING", 1,
                                       "No refresh in the last 36h" if last else "Never refreshed."))
    return issues


__all__ = ["QualityIssue", "run_data_quality_checks", "STALE_AFTER"]
