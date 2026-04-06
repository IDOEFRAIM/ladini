from __future__ import annotations

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.operators.bash import BashOperator


default_args = {
    "owner": "agriconnect",
    "depends_on_past": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}


with DAG(
    dag_id="Main_Ingestion_DAG",
    default_args=default_args,
    description="Compatibility wrapper that delegates ingestion to S3+RDS agri_ingestion_master",
    schedule="@hourly",
    start_date=datetime(2026, 3, 20),
    catchup=False,
    max_active_runs=1,
    tags=["ingestion", "compat", "s3", "rds"],
) as dag:
    trigger_sync = TriggerDagRunOperator(
        task_id="trigger_sync_raw_to_s3",
        trigger_dag_id="sync_raw_to_s3",
        conf={"triggered_by": "Main_Ingestion_DAG"},
        wait_for_completion=False,
    )

    run_worker = BashOperator(
        task_id="run_ingestion_worker_once",
        bash_command="python -m backend.ingestion.worker --once --s3-prefix-filter raw_data/weather_advisories",
    )

    trigger_master = TriggerDagRunOperator(
        task_id="trigger_agri_ingestion_master",
        trigger_dag_id="agri_ingestion_master",
        conf={
            "triggered_by": "Main_Ingestion_DAG",
            "mode": "s3_rds_only",
        },
        wait_for_completion=False,
    )

    trigger_sync >> run_worker >> trigger_master
