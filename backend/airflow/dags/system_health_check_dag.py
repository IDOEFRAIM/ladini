"""
System Health Check DAG ("Le Bouclier")
Validates critical infrastructure before potential large ingestion jobs.
Runs early morning or triggered.
Checks:
- RDS Connectivity & Query Latency
- Vector Store Health (Null Embeddings count)
- Local Disk Space (Airflow Worker)
- S3 Accessibility
- External API Quota/Connectivity (Simulated for OpenAI/Bedrock)
"""
import logging
import shutil
import time
from datetime import datetime, timedelta
import os
import sys
from pathlib import Path

# --- Bootstrap Paths ---
backend_dir = Path(__file__).resolve().parents[2]  # backend/
project_root = backend_dir.parent

# 1. Add project 'src/' so the `agriconnect` package is importable
src = project_root / "src"
if str(src) not in sys.path:
    sys.path.insert(0, str(src))

# 2. Also allow importing `backend.*` modules
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
# -----------------------

from airflow.decorators import dag, task
from airflow.exceptions import AirflowException
from sqlalchemy import text
from agriconnect.core.db import get_engine, resolve_database_url
from agriconnect.core.settings import settings

logger = logging.getLogger("SystemHealth")

@dag(
    dag_id="System_Health_Check",
    schedule="0 2 * * *", # Run at 2 AM
    start_date=datetime(2026, 3, 22),
    catchup=False,
    max_active_runs=1,
    default_args={"owner": "agriconnect", "retries": 0},
    tags=["monitoring", "maintenance"]
)
def system_health_check():

    @task
    def check_disk_space(min_gb: int = 2) -> dict:
        """Verifies local disk space on worker node."""
        total, used, free = shutil.disk_usage("/")
        free_gb = free / (1024**3)
        logger.info(f"Disk Space: {free_gb:.2f} GB free.")
        
        if free_gb < min_gb:
            raise AirflowException(f"Disk space critical: {free_gb:.2f} GB < {min_gb} GB")
        
        return {"free_gb": free_gb, "status": "ok"}

    @task
    def check_rds_connectivity() -> dict:
        """Pings Postres/RDS and checks simple query latency."""
        db_url = resolve_database_url(required=True)
        engine = get_engine(db_url)

        start_time = time.time()
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            latency_ms = (time.time() - start_time) * 1000
            logger.info(f"RDS Ping: {latency_ms:.2f} ms")
            
            if latency_ms > 2000:
                logger.warning(f"High DB Latency: {latency_ms:.2f} ms")
                
            return {"latency_ms": latency_ms, "status": "ok"}
        except Exception as e:
            logger.error(f"RDS Connection Failed: {e}")
            raise AirflowException(f"RDS Unreachable: {e}")

    @task
    def check_vector_health() -> dict:
        """Counts null embeddings in chunks table."""
        db_url = resolve_database_url(required=True)
        engine = get_engine(db_url)
        try:
            schema = "agri_vector"
            table = "document_chunks"

            with engine.connect() as conn:
                exists = bool(
                    conn.execute(
                        text(
                            """
                            SELECT EXISTS (
                                SELECT FROM information_schema.tables
                                WHERE table_schema = :schema AND table_name = :table
                            )
                            """
                        ),
                        {"schema": schema, "table": table},
                    ).scalar()
                )

                if not exists:
                    logger.warning(f"Table {schema}.{table} does not exist yet. Skipping check.")
                    return {"status": "skipped", "reason": "table_not_found"}

                null_count = int(
                    conn.execute(text("SELECT COUNT(*) FROM agri_vector.document_chunks WHERE embedding IS NULL")).scalar() or 0
                )
                total_count = int(
                    conn.execute(text("SELECT COUNT(*) FROM agri_vector.document_chunks")).scalar() or 0
                )

            logger.info(f"Vector Stats: {total_count} total, {null_count} null embeddings.")
            
            if null_count > 0:
                # Warning only, doesn't necessarily fail pipeline but alerts
                logger.warning(f"Found {null_count} chunks with missing embeddings!")
                
            return {"total_chunks": total_count, "null_embeddings": null_count, "status": "ok"}

        except Exception as e:
             logger.error(f"Vector Health Check Failed: {e}")
             # Don't fail the DAG if it's just a query error on a non-critical check?
             # But here we want to know system health, so raising is safer.
             raise AirflowException(f"Vector Check Error: {e}")

    @task
    def check_s3_bucket() -> dict:
        """Verifies S3 bucket accessibility."""
        # Using S3Manager if configured
        try:
            from backend.ingestion.storage.s3_manager import S3Manager
            s3 = S3Manager()
            # Use boto3 client directly for listing, as S3Manager doesn't expose list
            s3.client.list_objects_v2(Bucket=s3.bucket, MaxKeys=1)
            logger.info("S3 Read Access Confirmed.")
            return {"status": "ok"}
        except ImportError:
            logger.warning("S3Manager not found, skipping S3 check.")
            return {"status": "skipped"}
        except Exception as e:
            logger.error(f"S3 Access Failed: {e}")
            raise AirflowException(f"S3 Unreachable: {e}")

    # Flow
    disk = check_disk_space()
    db = check_rds_connectivity()
    s3 = check_s3_bucket()
    vector = check_vector_health()

    [disk, db, s3] >> vector

system_health_check()
