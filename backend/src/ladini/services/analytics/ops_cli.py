"""Operator CLI for the Phase E production rollout of Buyer Analytics.

Run inside the `worker` container (same image/env as the Celery worker, so `init_db()` resolves the
real `DATABASE_URL`), via the officially deployed compose stack — never against a hand-run DB
connection. Every subcommand prints one JSON object to stdout and nothing else identifying (no
tokens, no phone numbers, no raw rows) — safe to capture in a CI job log.

    python -m ladini.services.analytics.ops_cli recompute --lookback-days 14
    python -m ladini.services.analytics.ops_cli drift
    python -m ladini.services.analytics.ops_cli backfill
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone

from ladini.domain.analytics.daily_aggregation import (
    resolve_canonical,
    to_canonical_quantity,
)
from ladini.services.analytics.analytics_service import AnalyticsService
from ladini.services.analytics.daily_metrics_refresh import (
    DIRECT_COHORT_TS,
    DIRECT_ORDER_WHERE,
    backfill_quantity_delivered,
    recompute_recent,
)
from ladini.services.analytics.data_quality import run_data_quality_checks
from ladini.workers.runtime import worker_session

_DRIFT_SQL = (
    "SELECT count(*) FROM marketplace.recurring_need_occurrences oc "
    "LEFT JOIN ("
    "  SELECT na.occurrence_id AS occurrence_id, sum(oi.quantity) AS q"
    "  FROM marketplace.need_allocations na"
    "  JOIN marketplace.order_items oi ON oi.id = na.order_item_id"
    "  JOIN marketplace.orders o ON o.id = oi.order_id"
    "  WHERE na.status = 'CONVERTED' AND o.order_type = 'RECURRING_SUPPLY' AND o.delivery_status = 'RECEIVED'"
    "  GROUP BY na.occurrence_id"
    ") d ON d.occurrence_id = oc.id "
    "WHERE oc.quantity_delivered <> COALESCE(d.q, 0)"
)


async def cmd_recompute(lookback_days: int) -> dict:
    result = await recompute_recent(worker_session, lookback_days)
    return {"action": "recompute", "lookback_days": lookback_days, "days_recomputed": len(result), "per_day_rows": result}


async def cmd_drift() -> dict:
    from sqlalchemy import text

    async with worker_session() as session:
        n = int((await session.execute(text(_DRIFT_SQL))).scalar() or 0)
    return {"action": "drift", "occurrences_diverging": n}


async def cmd_backfill() -> dict:
    async with worker_session() as session:
        updated = await backfill_quantity_delivered(session)
    return {"action": "backfill", "occurrences_updated": updated}


async def cmd_quality(days: int) -> dict:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days - 1)
    async with worker_session() as session:
        issues = await run_data_quality_checks(session, start, end)
    return {
        "action": "quality", "window": {"start": start.isoformat(), "end": end.isoformat()},
        "issue_count": len(issues),
        "issues": [{"check": i.check, "table": i.table, "severity": i.severity, "count": i.count} for i in issues],
    }


def _close(a, b, tol: float = 0.01) -> bool:
    if a is None or b is None:
        return bool(a == b)
    return abs(float(a) - float(b)) <= tol


async def cmd_verify_kpis(days: int) -> dict:
    """Cross-check the daily aggregates (what the dashboard reads) against a FRESH pull from the
    transactional/business_events source of truth, over the same window — proves the stored
    aggregate isn't stale or wrong, not just that the code runs. Never a comparison against itself:
    every `source_*` value below is computed by an independent query, not by re-reading
    `analytics.*_daily_metrics`."""
    from sqlalchemy import text

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days - 1)
    start_dt, end_dt = datetime.combine(start, datetime.min.time()), datetime.combine(end + timedelta(days=1), datetime.min.time())
    start_tz, end_tz = start_dt.replace(tzinfo=timezone.utc), end_dt.replace(tzinfo=timezone.utc)
    period = {"start": start.isoformat(), "end": end.isoformat()}
    checks = []

    async with worker_session() as session:
        svc = AnalyticsService(session)

        # 1. active_buyers
        dashboard = await svc.get_metric("active_buyers", start, end, compare=False)
        source = await session.scalar(text(
            "SELECT count(DISTINCT buyer_id) FROM ("
            f"  SELECT buyer_id FROM marketplace.orders o WHERE {DIRECT_ORDER_WHERE} AND {DIRECT_COHORT_TS} >= :s AND {DIRECT_COHORT_TS} < :e"
            "  UNION SELECT buyer_id FROM marketplace.auctions WHERE created_at >= :s AND created_at < :e"
            "  UNION SELECT n.buyer_id FROM marketplace.recurring_need_occurrences oc JOIN marketplace.recurring_needs n ON n.id = oc.recurring_need_id"
            "    WHERE oc.status NOT IN ('SKIPPED','CANCELLED') AND oc.occurrence_date >= :s AND oc.occurrence_date < :e"
            ") x"
        ), {"s": start_dt, "e": end_dt})
        checks.append({"metric": "active_buyers", "dashboard": dashboard.value, "source": source, "match": _close(dashboard.value, source)})

        # 2. needs_created
        dashboard = await svc.get_metric("needs_created", start, end, compare=False)
        source = await session.scalar(text(
            f"SELECT (SELECT count(*) FROM marketplace.orders o WHERE {DIRECT_ORDER_WHERE} AND {DIRECT_COHORT_TS} >= :s AND {DIRECT_COHORT_TS} < :e)"
            " + (SELECT count(*) FROM marketplace.auctions WHERE created_at >= :s AND created_at < :e)"
            " + (SELECT count(*) FROM marketplace.recurring_need_occurrences WHERE status NOT IN ('SKIPPED','CANCELLED') AND occurrence_date >= :s AND occurrence_date < :e)"
        ), {"s": start_dt, "e": end_dt})
        checks.append({"metric": "needs_created", "dashboard": dashboard.value, "source": source, "match": dashboard.value == source})

        # 3. direct_search_success_rate — straight from business_events, bypassing direct_daily_metrics.
        dashboard = await svc.get_metric("direct_search_success_rate", start, end, compare=False)
        row = (await session.execute(text(
            "SELECT count(*) FILTER (WHERE event_name = 'DIRECT_SEARCH_PERFORMED') AS performed,"
            "       count(*) FILTER (WHERE event_name = 'DIRECT_SEARCH_SUCCEEDED') AS succeeded"
            " FROM analytics.business_events WHERE journey = 'DIRECT' AND occurred_at >= :s AND occurred_at < :e"
        ), {"s": start_tz, "e": end_tz})).mappings().one()
        src_value = (row["succeeded"] / row["performed"]) if row["performed"] else None
        checks.append({"metric": "direct_search_success_rate", "dashboard": dashboard.value,
                       "source_numerator": row["succeeded"], "source_denominator": row["performed"],
                       "source": src_value, "match": _close(dashboard.value, src_value)})

        # 4. tender_response_rate
        dashboard = await svc.get_metric("tender_response_rate", start, end, compare=False)
        row = (await session.execute(text(
            "SELECT count(*) AS created, count(*) FILTER (WHERE EXISTS ("
            "  SELECT 1 FROM marketplace.bids b WHERE b.auction_id = a.id)) AS with_bid"
            " FROM marketplace.auctions a WHERE a.created_at >= :s AND a.created_at < :e"
        ), {"s": start_dt, "e": end_dt})).mappings().one()
        src_value = (row["with_bid"] / row["created"]) if row["created"] else None
        checks.append({"metric": "tender_response_rate", "dashboard": dashboard.value,
                       "source_numerator": row["with_bid"], "source_denominator": row["created"],
                       "source": src_value, "match": _close(dashboard.value, src_value)})

        # 5. recurring_coverage_rate — independent re-aggregation from RAW occurrence rows (not from
        # the stored `recurring_daily_metrics` table), grouped exactly like the dashboard breakdown
        # (sub_category_id x canonical_unit) using the same pure, separately-unit-tested conversion
        # helper (`resolve_canonical`/`to_canonical_quantity`) — never a same-unit-only shortcut, so
        # a real G->KG occurrence is included on both sides instead of silently excluded from one.
        dashboard_rows = await svc.get_metric_breakdown("recurring_coverage_rate", start, end, dimension="sub_category_id")
        dashboard_by_group = {(str(r["sub_category_id"]), r["canonical_unit"]): r for r in dashboard_rows}
        raw_rows = (await session.execute(text(
            "SELECT n.sub_category_id, oc.unit, oc.requested_quantity, oc.quantity_matched, sc.priority_unit"
            " FROM marketplace.recurring_need_occurrences oc JOIN marketplace.recurring_needs n ON n.id = oc.recurring_need_id"
            " JOIN governance.sub_categories sc ON sc.id = n.sub_category_id"
            " WHERE oc.status NOT IN ('SKIPPED','CANCELLED') AND oc.occurrence_date >= :s AND oc.occurrence_date < :e"
        ), {"s": start_dt, "e": end_dt})).mappings().all()
        source_by_group: dict = {}
        for r in raw_rows:
            canonical = resolve_canonical(r["unit"], r["priority_unit"])
            g = source_by_group.setdefault((str(r["sub_category_id"]), canonical), {"requested": 0.0, "matched": 0.0})
            g["requested"] += float(to_canonical_quantity(r["requested_quantity"], r["unit"], canonical))
            g["matched"] += float(to_canonical_quantity(r["quantity_matched"], r["unit"], canonical))
        group_mismatches = [
            {"sub_category_id": k[0], "canonical_unit": k[1], "dashboard": dashboard_by_group.get(k), "source": v}
            for k, v in source_by_group.items()
            if not (dashboard_by_group.get(k) and _close(dashboard_by_group[k]["numerator"], v["matched"]) and _close(dashboard_by_group[k]["denominator"], v["requested"]))
        ] + [{"sub_category_id": k[0], "canonical_unit": k[1], "dashboard": v, "source": None} for k, v in dashboard_by_group.items() if k not in source_by_group]
        checks.append({"metric": "recurring_coverage_rate", "groups_compared": len(source_by_group),
                       "group_mismatches": group_mismatches, "match": not group_mismatches})

        # 6. unmatched demand for one sub-category (the largest one in the window, if any).
        unmatched = await svc.get_unfulfilled_demand(start, end, group_by=("sub_category_id",))
        top = max(unmatched["rows"], key=lambda r: r["unmatched"], default=None)
        if top is not None:
            source_unmatched = await session.scalar(text(
                "SELECT COALESCE(sum(GREATEST(oc.requested_quantity - oc.quantity_matched, 0)), 0)"
                " FROM marketplace.recurring_need_occurrences oc JOIN marketplace.recurring_needs n ON n.id = oc.recurring_need_id"
                " WHERE oc.status NOT IN ('SKIPPED','CANCELLED') AND n.sub_category_id = :sub AND oc.unit = :unit"
                " AND oc.occurrence_date >= :s AND oc.occurrence_date < :e"
            ), {"sub": top["sub_category_id"], "unit": top["canonical_unit"], "s": start_dt, "e": end_dt})
            checks.append({"metric": "unmatched_demand", "sub_category_id": top["sub_category_id"], "canonical_unit": top["canonical_unit"],
                           "dashboard": top["unmatched"], "source": float(source_unmatched), "match": _close(top["unmatched"], float(source_unmatched))})
        else:
            checks.append({"metric": "unmatched_demand", "dashboard": None, "source": None, "match": True, "note": "no recurring demand in this window"})

    return {"action": "verify_kpis", "window": period, "all_match": all(c["match"] for c in checks), "checks": checks}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_recompute = sub.add_parser("recompute", help="Recompute the daily aggregates over a rolling window.")
    p_recompute.add_argument("--lookback-days", type=int, default=14)
    sub.add_parser("drift", help="Count recurring occurrences whose quantity_delivered disagrees with the RECEIVED-order source of truth.")
    sub.add_parser("backfill", help="Idempotently realign quantity_delivered from the source of truth (recompute, never increment).")
    p_quality = sub.add_parser("quality", help="Run the data-quality checks over a window.")
    p_quality.add_argument("--days", type=int, default=30)
    p_verify = sub.add_parser("verify-kpis", help="Cross-check dashboard metrics against a fresh transactional-source computation.")
    p_verify.add_argument("--days", type=int, default=14)
    args = parser.parse_args()

    handlers = {
        "recompute": lambda: cmd_recompute(args.lookback_days),
        "drift": cmd_drift,
        "backfill": cmd_backfill,
        "quality": lambda: cmd_quality(args.days),
        "verify-kpis": lambda: cmd_verify_kpis(args.days),
    }
    result = asyncio.run(handlers[args.command]())
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
