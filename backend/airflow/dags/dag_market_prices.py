from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow.decorators import dag, task  # type: ignore[reportMissingImports]


def _bootstrap_path() -> None:
    p = Path(__file__).resolve()
    for _ in range(6):
        candidate = p.parent
        if (candidate / "src" / "agriconnect").exists():
            sp = str(candidate / "src")
            if sp not in sys.path:
                sys.path.insert(0, sp)
            return
        if (candidate / "backend" / "src" / "agriconnect").exists():
            sp = str(candidate / "backend" / "src")
            if sp not in sys.path:
                sys.path.insert(0, sp)
            return
        p = candidate
    fallback = Path("/opt/airflow/agriconnect_root/backend/src")
    if fallback.exists():
        sf = str(fallback)
        if sf not in sys.path:
            sys.path.insert(0, sf)


@dag(
    dag_id="DAG_Market_Prices",
    schedule="0 */4 * * *",
    start_date=datetime(2026, 3, 19),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "agriconnect",
        "retries": 2,
        "retry_delay": timedelta(minutes=8),
    },
    tags=["agriconnect", "market", "pricing"],
)
def dag_market_prices():
    @task
    def collect_and_store_market_prices() -> dict:
        _bootstrap_path()
        from agriconnect.services.data_collection.market.market_collector import MarketCollector

        collector = MarketCollector()
        result = collector.run()
        return result

    collect_and_store_market_prices()


dag_market_prices()
