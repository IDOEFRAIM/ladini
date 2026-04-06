from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow.decorators import dag, task  # type: ignore[reportMissingImports]


def _bootstrap_path() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    src = repo_root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


@dag(
    dag_id="DAG_Market_Zone_Matching",
    schedule="0 5 * * *",
    start_date=datetime(2026, 3, 28),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "agriconnect",
        "retries": 1,
        "retry_delay": timedelta(minutes=10),
    },
    tags=["agriconnect", "market", "matching", "zone"],
)
def dag_market_zone_matching():
    @task
    def run_daily_matching(per_buyer_limit: int = 20) -> dict:
        _bootstrap_path()
        from agriconnect.services.notifications.matching import NotificationMatcher

        matcher = NotificationMatcher()
        return matcher.run_matching(per_buyer_limit=per_buyer_limit)

    @task
    def clean_stale_matches(max_age_days: int = 7) -> dict:
        _bootstrap_path()
        from agriconnect.services.notifications.matching import NotificationMatcher

        matcher = NotificationMatcher()
        return matcher.clean_expired_matches(max_age_days=max_age_days, hard_delete=True)

    matching = run_daily_matching()
    cleanup = clean_stale_matches()
    matching >> cleanup


dag_market_zone_matching()
