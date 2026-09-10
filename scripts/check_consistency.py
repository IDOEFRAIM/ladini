#!/usr/bin/env python3
"""
Check consistency between Postgres ingestion.document_chunks is_indexed flag and total chunks.
Usage: set DATABASE_URL env var or ensure project config resolves the DB URL.
"""
import os
import sys
from sqlalchemy import create_engine, text

DB_URL = os.getenv("DATABASE_URL") or os.getenv("AGRICONNECT_DATABASE_URL")
if not DB_URL:
    print("DATABASE_URL not set", file=sys.stderr)
    sys.exit(2)

engine = create_engine(DB_URL)
with engine.connect() as conn:
    total_q = text("SELECT count(1) FROM ingestion.document_chunks")
    indexed_q = text("SELECT count(1) FROM ingestion.document_chunks WHERE is_indexed = TRUE")
    null_q = text("SELECT count(1) FROM ingestion.document_chunks WHERE is_indexed IS NULL")

    total = conn.execute(total_q).scalar() or 0
    indexed = conn.execute(indexed_q).scalar() or 0
    nulls = conn.execute(null_q).scalar() or 0

    print(f"total_chunks={total}")
    print(f"indexed_chunks={indexed}")
    print(f"null_is_indexed={nulls}")

    if total != indexed:
        print("INCONSISTENCY: indexed != total. Consider running backfill or reconciliation.")
        sys.exit(1)
    print("OK: All chunks indexed.")
    sys.exit(0)
