from __future__ import annotations

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.trigger_dagrun import TriggerDagRunOperator


default_args = {
    "owner": "agriconnect",
    "depends_on_past": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}


with DAG(
    dag_id="sync_raw_to_s3",
    default_args=default_args,
    description="Compatibility wrapper that triggers S3-only agri_ingestion_master DAG",
    schedule="@hourly",
    start_date=datetime(2026, 3, 20),
    catchup=False,
    tags=["ingestion", "s3", "compat"],
) as dag:
    trigger_master = TriggerDagRunOperator(
        task_id="trigger_agri_ingestion_master",
        trigger_dag_id="agri_ingestion_master",
        conf={
            "triggered_by": "sync_raw_to_s3",
            "mode": "s3_only",
        },
        wait_for_completion=False,
    )
