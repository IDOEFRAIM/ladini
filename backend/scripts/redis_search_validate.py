"""Validate RedisSearch (Redis Stack) vector index end-to-end.

Usage:
  - Set `REDIS_URL` in env to a Redis Stack endpoint with vector support.
  - Run: python backend/scripts/redis_search_validate.py

The script will create a test document, query it and clean up.
"""
from __future__ import annotations

import os
import sys
import logging
import json
from typing import Optional

try:
    import numpy as np
except Exception:
    np = None

try:
    from agriconnect.rag.redis_search_store import RedisSearchVectorStore
except Exception as e:
    RedisSearchVectorStore = None
    _import_err = e

from agriconnect.core.settings import settings

logger = logging.getLogger("redis_search_validate")


def main(redis_url: Optional[str] = None) -> int:
    url = redis_url or os.getenv("REDIS_URL") or getattr(settings, "REDIS_URL", None)
    if not url:
        print("Set REDIS_URL to a Redis Stack endpoint and retry (or pass --redis-url).")
        return 2

    if RedisSearchVectorStore is None:
        print("RedisSearchVectorStore not available:", _import_err)
        return 3

    if np is None:
        print("Install numpy to run validation (pip install numpy).")
        return 4

    try:
        store = RedisSearchVectorStore(url, dim=128)
    except Exception as e:
        print("Could not instantiate RedisSearchVectorStore:", e)
        return 5

    # verify connection early
    try:
        if hasattr(store, "client"):
            store.client.ping()
    except Exception as e:
        print("Cannot connect to Redis at the provided URL:", e)
        print("Ensure you provided a Redis Stack endpoint (RedisSearch/vector support).")
        print("Run with --redis-url or set REDIS_URL to the correct endpoint.")
        return 6

    # create a deterministic test vector
    vec = (np.arange(store.dim, dtype=np.float32) % 10) / 10.0
    doc_id = "validate-redissearch-1"
    try:
        store.add(doc_id, "validation test", {"source": "validate"}, vec.tolist())
        print("Added test doc", doc_id)
        res = store.query(vec.tolist(), k=1)
        print("Query results:", json.dumps(res, indent=2))
        found = any(r.get("id") == doc_id for r in res)
        store.delete(doc_id)
        if found:
            print("Validation succeeded: query returned the test document")
            return 0
        else:
            print("Validation failed: test document not found in query results")
            return 7
    except Exception as e:
        logger.exception("Validation run failed: %s", e)
        print("Validation run failed:", e)
        return 8


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    p.add_argument("--redis-url", help="Redis connection URL to use for validation")
    args = p.parse_args()
    sys.exit(main(redis_url=args.redis_url))
