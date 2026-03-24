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
    dag_id="DAG_User_Notification",
    schedule="15 * * * *",
    start_date=datetime(2026, 3, 19),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "agriconnect",
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
    },
    tags=["agriconnect", "notifications", "matching"],
)
def dag_user_notification():
    @task
    def match_user_context() -> dict:
        _bootstrap_path()
        from agriconnect.services.notifications.matching import NotificationMatcher

        matcher = NotificationMatcher()
        return matcher.run_matching()

    @task
    def dispatch_notifications(match_result: dict, limit: int = 250) -> dict:
        _bootstrap_path()
        from agriconnect.services.notifications.matching import NotificationMatcher

        matcher = NotificationMatcher()
        sent = matcher.dispatch_pending(limit=limit)
        return {
            "matched": match_result.get("messages", 0),
            "persisted": match_result.get("persisted", 0),
            "sent": sent,
        }

    dispatch_notifications(match_user_context())


dag_user_notification()
