# Compatibility wrapper for legacy ingestor implementation
try:
    from backend._legacy.ingestor_archived import Ingestor  # type: ignore
except Exception:
    # Re-raise with clearer message for Airflow logs
    raise
