from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow.decorators import dag, task  # type: ignore[reportMissingImports]
from sqlalchemy import text


def _bootstrap_path() -> None:
    backend_dir = Path(__file__).resolve().parents[2]
    src = backend_dir / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


@dag(
    dag_id="DAG_GC_Old_Chunk_Versions",
    schedule="30 2 * * *",
    start_date=datetime(2026, 3, 19),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "agriconnect",
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
    },
    tags=["agriconnect", "gc", "pgvector", "chunks"],
)
def dag_gc_old_chunk_versions():
    @task
    def deactivate_expired_chunks() -> dict:
        _bootstrap_path()
        from agriconnect.core.db import get_engine, resolve_database_url

        engine = get_engine(resolve_database_url(required=True))
        with engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE agri_vector.document_chunks
                    SET is_active = FALSE,
                        updated_at = NOW()
                    WHERE is_active = TRUE
                      AND valid_until IS NOT NULL
                      AND valid_until < NOW()
                    """
                )
            )
            deactivated = int(result.rowcount or 0)
        return {"deactivated": deactivated}

    @task
    def purge_old_inactive_versions(retention_days: int = 7) -> dict:
        _bootstrap_path()
        from agriconnect.core.db import get_engine, resolve_database_url

        engine = get_engine(resolve_database_url(required=True))
        with engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    DELETE FROM agri_vector.document_chunks old
                    WHERE old.is_active = FALSE
                      AND old.updated_at < NOW() - make_interval(days => :retention_days)
                      AND EXISTS (
                          SELECT 1
                          FROM agri_vector.document_chunks newer
                          WHERE newer.doc_ref = old.doc_ref
                            AND newer.is_active = TRUE
                            AND newer.chunk_version >= old.chunk_version
                      )
                    """
                ),
                {"retention_days": int(retention_days)},
            )
            purged = int(result.rowcount or 0)
        return {"purged": purged, "retention_days": int(retention_days)}

    purge_old_inactive_versions(deactivate_expired_chunks())


dag_gc_old_chunk_versions()
