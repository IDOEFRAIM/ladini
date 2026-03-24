from __future__ import annotations

import sys
import os
from pathlib import Path

# Windows compatibility for Airflow
if sys.platform == 'win32':
    import unittest.mock
    # Mock fcntl
    fcntl = unittest.mock.Mock()
    fcntl.ioctl.return_value = b'\x00' * 8
    sys.modules['fcntl'] = fcntl
    sys.modules['termios'] = unittest.mock.Mock()
    sys.modules['tty'] = unittest.mock.Mock()
    
    # Set AIRFLOW_HOME BEFORE importing airflow
    repo_root = Path(__file__).resolve().parents[2]
    airflow_home = repo_root / "airflow_home"
    if not airflow_home.exists():
         airflow_home.mkdir(parents=True, exist_ok=True)
    os.environ["AIRFLOW_HOME"] = str(airflow_home)

    # Patch signal.SIGALRM and others
    import signal
    if not hasattr(signal, 'SIGALRM'):
        signal.SIGALRM = 14
    if not hasattr(signal, 'ITIMER_REAL'):
        signal.ITIMER_REAL = 0
    if not hasattr(signal, 'setitimer'):
        signal.setitimer = lambda *args, **kwargs: None
    
    # Patch airflow.utils.db.timeout_with_traceback
    import airflow.utils.db
    from contextlib import contextmanager
    @contextmanager
    def dummy_timeout(seconds, error_message=None):
        yield
    airflow.utils.db.timeout_with_traceback = dummy_timeout
    
    # Patch airflow.models.dagbag.timeout
    import airflow.models.dagbag
    airflow.models.dagbag.timeout = dummy_timeout

from datetime import datetime, timedelta
from pathlib import Path

from airflow.decorators import dag, task  # type: ignore[reportMissingImports]
from pathlib import Path


def _load_repo_env() -> None:
    try:
        # Assumes file is in backend/airflow/dags/
        repo_root = Path(__file__).resolve().parents[2] # Should be 'backend' folder
        env_path = repo_root / ".env"
        if not env_path.exists():
             # Try project root
             env_path = repo_root.parent / ".env"

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
    # Adds backend/src to path so 'agriconnect' can be imported
    repo_root = Path(__file__).resolve().parents[2]
    src = repo_root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    # Also add backend root to allow 'backend.ingestion' imports if needed
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


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
    tags=["agriconnect", "weather", "timeseries"],
)
def dag_ingest_weather():
    @task
    def collect_weather() -> dict:
        _load_repo_env()
        _bootstrap_path()
        from agriconnect.services.data_collection.weather.weather_collector import WeatherCollector

        collector = WeatherCollector()
        result = collector.run()
        return result

    @task
    def reconcile_sources(collection_result: dict) -> dict:
        _bootstrap_path()
        from pathlib import Path
        from agriconnect.core.settings import settings
        from agriconnect.services.data_collection.weather.weather_collector import WeatherStorage
        from agriconnect.services.data_collection.weather.reconciliation import WeatherReconciler

        # Ensure we have a DB URL
        db_url = settings.DATABASE_URL
        storage = WeatherStorage(db_url, Path("."))
        reconciler = WeatherReconciler(storage)
        
        # Run reconciliation with write mode enabled
        result = reconciler.reconcile(write_reconciled=True)
        return result

    @task
    def refresh_vector_store(collection_result: dict, reconciliation_result: dict, chunk_size: int = 720, chunk_overlap: int = 100) -> dict:
        _bootstrap_path()
        from agriconnect.rag.ingestor import Ingestor

        ingestor = Ingestor(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        index = ingestor.build_index(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        return {
            "weather_signals": collection_result.get("signals", 0),
            "alerts": collection_result.get("alerts", 0),
            "advisory_docs": collection_result.get("advisory_docs", 0),
            "reconciled_written": reconciliation_result.get("reconciled_written", 0),
            "zones_compared": reconciliation_result.get("zones_compared", 0),
            "index_updated": bool(index),
        }

    cw_res = collect_weather()
    rec_res = reconcile_sources(cw_res)
    refresh_vector_store(cw_res, rec_res)


dag = dag_ingest_weather()

if __name__ == "__main__":
    dag.test()
