"""Domain-level ingestion orchestration API.

This module exposes simple functions that Airflow DAGs can call
without containing business logic themselves.
"""
from __future__ import annotations

from typing import Any, Dict
import logging

from agriconnect.core.settings import settings

logger = logging.getLogger(__name__)


def run_weather_pipeline(mode: str = "s3_rds_only") -> Dict[str, Any]:
    """Run the full weather ingestion pipeline (collect, reconcile, persist).

    The function keeps orchestration logic here so DAGs remain thin.
    """
    # Import locally to avoid heavy imports at module import time
    # Use service-level data_collection (scraper-centric) instead of domain adapters
    from futur.data_collection.weather.weather_collector import WeatherCollector
    from futur.data_collection.weather.reconciliation import WeatherReconciler
    from futur.data_collection.weather.weather_collector import WeatherStorage

    if mode == "s3_rds_only":
        if not settings.DATABASE_URL:
            raise RuntimeError("DATABASE_URL must be configured for weather pipeline")
        if not settings.S3_BUCKET:
            raise RuntimeError("S3_BUCKET must be configured for weather pipeline")

    collector = WeatherCollector()
    collected = collector.run()

    storage = WeatherStorage(settings.DATABASE_URL)
    reconciler = WeatherReconciler(storage)
    reconciled = reconciler.reconcile(write_reconciled=True)

    return {"collected": collected, "reconciled": reconciled}


def run_ingestion_master() -> Dict[str, Any]:
    """Placeholder to run the master ingestion orchestration (calls specific pipelines).

    Add other pipelines here as needed.
    """
    results = {}
    results["weather"] = run_weather_pipeline()
    return results
