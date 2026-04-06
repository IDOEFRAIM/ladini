from __future__ import annotations

import sys
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

from airflow.decorators import dag, task  # type: ignore[reportMissingImports]


def _bootstrap_path() -> None:
    backend_dir = Path(__file__).resolve().parents[2]
    
    # 1. Add project 'backend/src' so the `agriconnect` package is importable
    src = backend_dir / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    # 2. Add backend scripts path for migration helper scripts
    scripts = backend_dir / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))


@dag(
    dag_id="DAG_Migrate_RAG_JSON_To_PGVector",
    schedule=None,
    start_date=datetime(2026, 3, 19),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "agriconnect",
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
    tags=["agriconnect", "migration", "pgvector", "one-shot"],
)
def dag_migrate_rag_json_to_pgvector():
    @task
    def run_one_shot_migration(dry_run: bool = False, batch_size: int = 500) -> dict:
        _bootstrap_path()
        repo_root = Path(__file__).resolve().parents[2]
        script_path = repo_root / "scripts" / "migrate_rag_db_to_pgvector.py"
        spec = importlib.util.spec_from_file_location("migrate_rag_db_to_pgvector", script_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Unable to load migration module from {script_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        run_migration = getattr(module, "run_migration")

        return run_migration(dry_run=dry_run, batch_size=batch_size)

    run_one_shot_migration()


dag_migrate_rag_json_to_pgvector()
