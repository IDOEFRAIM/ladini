#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make backend/src importable when executed from backend/scripts
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agriconnect.core.cloud_settings import get_settings


def main() -> int:
    settings = get_settings()
    result = {
        "s3": {"ok": False, "details": ""},
        "rds": {"ok": False, "details": ""},
        "pgvector": {"ok": False, "details": ""},
    }

    # S3 check
    try:
        import boto3
        from botocore.config import Config

        if not settings.S3_BUCKET:
            raise RuntimeError("S3_BUCKET not configured")

        session = boto3.session.Session(**settings.boto3_session_kwargs())
        s3 = session.client("s3", config=Config(retries={"max_attempts": 8, "mode": "adaptive"}))
        prefix = settings.RAW_DATA_PREFIX or "raw_data/"
        resp = s3.list_objects_v2(Bucket=settings.S3_BUCKET, Prefix=prefix, MaxKeys=1)
        result["s3"]["ok"] = True
        result["s3"]["details"] = {
            "bucket": settings.S3_BUCKET,
            "prefix": prefix,
            "found": int(resp.get("KeyCount", 0)),
        }
    except Exception as exc:
        result["s3"]["details"] = str(exc)

    # RDS + pgvector check
    try:
        import psycopg2

        db_url = settings.resolve_database_url()
        if not db_url:
            raise RuntimeError("DATABASE_URL unresolved")

        conn = psycopg2.connect(db_url)
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
            result["rds"]["ok"] = True
            result["rds"]["details"] = "SELECT 1 passed"

            cur.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector')")
            has_vector = bool(cur.fetchone()[0])
            result["pgvector"]["ok"] = has_vector
            result["pgvector"]["details"] = "installed" if has_vector else "not installed"
        conn.close()
    except Exception as exc:
        result["rds"]["details"] = str(exc)

    print(json.dumps(result, ensure_ascii=False, indent=2))

    if not (result["s3"]["ok"] and result["rds"]["ok"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
