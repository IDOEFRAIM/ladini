from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow.decorators import dag, task  # type: ignore[reportMissingImports]
import os
from pathlib import Path


def _load_repo_env() -> None:
    try:
        repo_root = Path(__file__).resolve().parents[2]
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
        pass


def _bootstrap_path() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    src = repo_root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


@dag(
    dag_id="DAG_Ingest_Docs",
    schedule="*/30 * * * *",
    start_date=datetime(2026, 3, 19),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "agriconnect",
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
    },
    tags=["agriconnect", "ingestion", "docs"],
)
def dag_ingest_docs():
    @task
    def ingest_docs(chunk_size: int = 650, chunk_overlap: int = 120, force_reingest: bool = False) -> dict:
        _load_repo_env()
        _bootstrap_path()
        from agriconnect.rag.ingestor import Ingestor

        # Airflow can pass chunk_size/chunk_overlap via dag_run.conf.
        env_chunk = os.getenv("AGRICONNECT_CHUNK_SIZE")
        env_overlap = os.getenv("AGRICONNECT_CHUNK_OVERLAP")
        resolved_chunk_size = int(env_chunk) if env_chunk else int(chunk_size)
        resolved_chunk_overlap = int(env_overlap) if env_overlap else int(chunk_overlap)

        ingestor = Ingestor(chunk_size=resolved_chunk_size, chunk_overlap=resolved_chunk_overlap)
        index = ingestor.build_index(
            chunk_size=resolved_chunk_size,
            chunk_overlap=resolved_chunk_overlap,
            force_reingest=force_reingest,
        )
        return {
            "index_updated": bool(index),
            "chunk_size": resolved_chunk_size,
            "chunk_overlap": resolved_chunk_overlap,
            "force_reingest": force_reingest,
        }

    ingest_docs()


dag_ingest_docs()
