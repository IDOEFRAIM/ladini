from __future__ import annotations

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.trigger_dagrun import TriggerDagRunOperator


default_args = {
    "owner": "agriconnect",
    "depends_on_past": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}


with DAG(
    dag_id="DAG_Ingest_Docs",
    default_args=default_args,
    description="Cloud-only doc ingestion wrapper (S3 -> pgvector on RDS)",
    schedule="*/30 * * * *",
    start_date=datetime(2026, 3, 19),
    catchup=False,
    max_active_runs=1,
    tags=["agriconnect", "ingestion", "docs", "s3", "rds"],
) as dag:
    trigger_master = TriggerDagRunOperator(
        task_id="trigger_agri_ingestion_master",
        trigger_dag_id="agri_ingestion_master",
        conf={
            "triggered_by": "DAG_Ingest_Docs",
            "mode": "s3_rds_only",
        },
        wait_for_completion=False,
    )
