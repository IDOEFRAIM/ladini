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
