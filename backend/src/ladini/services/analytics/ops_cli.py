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

from ladini.services.analytics.daily_metrics_refresh import (
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_recompute = sub.add_parser("recompute", help="Recompute the daily aggregates over a rolling window.")
    p_recompute.add_argument("--lookback-days", type=int, default=14)
    sub.add_parser("drift", help="Count recurring occurrences whose quantity_delivered disagrees with the RECEIVED-order source of truth.")
    sub.add_parser("backfill", help="Idempotently realign quantity_delivered from the source of truth (recompute, never increment).")
    p_quality = sub.add_parser("quality", help="Run the data-quality checks over a window.")
    p_quality.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    handlers = {
        "recompute": lambda: cmd_recompute(args.lookback_days),
        "drift": cmd_drift,
        "backfill": cmd_backfill,
        "quality": lambda: cmd_quality(args.days),
    }
    result = asyncio.run(handlers[args.command]())
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
