from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import runpy
import logging

from airflow import DAG
from airflow.operators.python import PythonOperator
import os
from pathlib import Path
import logging


def _load_repo_env():
    # If backend/.env exists in the repo (assumes DAGs folder under backend/airflow/dags),
    # load it into os.environ so tasks get AWS credentials when Airflow runs with the repo mounted.
    try:
        repo_root = Path(__file__).resolve().parents[3]
        env_path = repo_root / "backend" / ".env"
        if env_path.exists():
            with env_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"')
                        os.environ.setdefault(k, v)
    except Exception:
        logging.getLogger("airflow.sync_raw_to_s3").exception("Failed loading backend/.env")

logger = logging.getLogger("airflow.sync_raw_to_s3")


def _run_sync():
    _load_repo_env()
    # locate script relative to this file
    script = Path(__file__).resolve().parents[3] / "backend" / "scripts" / "sync_raw_to_s3.py"
    runpy.run_path(str(script), run_name="__main__")


def _run_scrapers():
    _load_repo_env()
    # run weather uploader (and any scrapers wired in run_weather_with_env)
    script = Path(__file__).resolve().parents[3] / "backend" / "scripts" / "run_weather_with_env.py"
    runpy.run_path(str(script), run_name="__main__")


def _run_redis_ingest():
    _load_repo_env()
    script = Path(__file__).resolve().parents[3] / "backend" / "scripts" / "run_redis_ingest_with_env.py"
    runpy.run_path(str(script), run_name="__main__")


default_args = {
    "owner": "agriconnect",
    "depends_on_past": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}

with DAG(
    dag_id="sync_raw_to_s3",
    default_args=default_args,
    description="Sync backend/sources/raw_data to S3",
    schedule_interval="@hourly",
    start_date=datetime(2026, 3, 20),
    catchup=False,
) as dag:

    def _sync_callable(force: bool = False) -> dict:
        """Wrapper to expose force flag via dag_run.conf and return a summary dict."""
        _load_repo_env()
        script = Path(__file__).resolve().parents[3] / "backend" / "scripts" / "sync_raw_to_s3.py"
        args = [str(script)]
        if force:
            args.append("--force")
        runpy.run_path(str(script), run_name="__main__")
        return {"synced": True, "force": force}


    def _scrapers_callable() -> dict:
        _load_repo_env()
        script = Path(__file__).resolve().parents[3] / "backend" / "scripts" / "run_weather_with_env.py"
        runpy.run_path(str(script), run_name="__main__")
        return {"scrapers_ran": True}


    def _ingest_callable(ingest_limit: int | None = None, force_reingest: bool = False) -> dict:
        _load_repo_env()
        script = Path(__file__).resolve().parents[3] / "backend" / "scripts" / "run_redis_ingest_with_env.py"
        # The helper script reads args from sys.argv; simulate by setting argv
        import sys

        argv = [str(script)]
        if ingest_limit:
            argv += ["--limit", str(ingest_limit)]
        if force_reingest:
            argv += ["--force-reingest"]
        sys.argv = argv
        runpy.run_path(str(script), run_name="__main__")
        return {"ingest_started": True, "limit": ingest_limit, "force_reingest": force_reingest}

    sync_task = PythonOperator(
        task_id="sync_raw_to_s3",
        python_callable=_sync_callable,
        op_kwargs={
            "force": "{{ dag_run.conf.get('force', False) if dag_run else False }}",
        },
        retries=2,
        retry_delay=timedelta(minutes=5),
        execution_timeout=timedelta(minutes=30),
    )

    scrapers_task = PythonOperator(
        task_id="run_scrapers",
        python_callable=_scrapers_callable,
        retries=1,
        retry_delay=timedelta(minutes=3),
        execution_timeout=timedelta(minutes=20),
    )

    ingest_task = PythonOperator(
        task_id="run_redis_ingest",
        python_callable=_ingest_callable,
        op_kwargs={
            "ingest_limit": "{{ dag_run.conf.get('limit') if dag_run else None }}",
            "force_reingest": "{{ dag_run.conf.get('force_reingest', False) if dag_run else False }}",
        },
        retries=1,
        retry_delay=timedelta(minutes=5),
        execution_timeout=timedelta(minutes=60),
    )

    # Sequence: sync -> scrapers -> ingest
    sync_task >> scrapers_task >> ingest_task
