from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow.decorators import dag, task  # type: ignore[reportMissingImports]
from airflow.operators.trigger_dagrun import TriggerDagRunOperator


def _load_repo_env() -> None:
    try:
        repo_root = Path(__file__).resolve().parents[2]
        env_path = repo_root / ".env"
        if not env_path.exists():
            env_path = repo_root.parent / ".env"

        if env_path.exists():
            with env_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip('"'))
    except Exception:
        pass


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
    dag_id="DAG_Ingest_Weather",
    schedule="0 * * * *",
    start_date=datetime(2026, 3, 19),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "agriconnect",
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
    },
    tags=["agriconnect", "weather", "timeseries", "s3", "rds"],
)
def dag_ingest_weather():
    @task
    def collect_weather() -> dict:
        _load_repo_env()
        _bootstrap_path()
        # Use domain-level pipeline API: keeps DAGs free of business logic
        from agriconnect.domain.ingestion import run_weather_pipeline

        return run_weather_pipeline()

    cw_res = collect_weather()
    rec_res = cw_res

    trigger_master = TriggerDagRunOperator(
        task_id="trigger_agri_ingestion_master",
        trigger_dag_id="agri_ingestion_master",
        conf={
            "triggered_by": "DAG_Ingest_Weather",
            "mode": "s3_rds_only",
        },
        wait_for_completion=False,
    )

    rec_res >> trigger_master


dag = dag_ingest_weather()
