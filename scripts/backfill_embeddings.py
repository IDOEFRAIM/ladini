#!/usr/bin/env python3
"""
Backfill embeddings for ingestion.document_chunks where is_indexed is NULL/false.
This script will publish embedding reindex jobs in batches by calling task_reindex_silver_document
or directly invoking the embed task depending on environment.
"""
import os
import sys
from sqlalchemy import create_engine, text
from math import ceil

DB_URL = os.getenv("DATABASE_URL") or os.getenv("AGRICONNECT_DATABASE_URL")
if not DB_URL:
    print("DATABASE_URL not set", file=sys.stderr)
    sys.exit(2)

BATCH_SIZE = int(os.getenv("BACKFILL_BATCH_SIZE", "50"))
REINDEX_TASK = os.getenv("USE_REINDEX_TASK", "1") in {"1", "true", "yes"}

engine = create_engine(DB_URL)

with engine.connect() as conn:
    count_q = text("SELECT count(DISTINCT file_hash) FROM ingestion.document_chunks WHERE COALESCE(is_indexed, false) = false")
    total = conn.execute(count_q).scalar() or 0
    if total == 0:
        print("No missing indexed documents found.")
        sys.exit(0)
    pages = ceil(int(total) / BATCH_SIZE)
    print(f"Found {total} file_hashes to reindex in {pages} batches of up to {BATCH_SIZE}.")

    fetch_q = text(
        "SELECT DISTINCT file_hash FROM ingestion.document_chunks WHERE COALESCE(is_indexed, false) = false ORDER BY file_hash LIMIT :limit OFFSET :offset"
    )

    for page in range(pages):
        offset = page * BATCH_SIZE
        rows = conn.execute(fetch_q, {"limit": BATCH_SIZE, "offset": offset}).fetchall()
        file_hashes = [r[0] for r in rows]
        for fh in file_hashes:
            if REINDEX_TASK:
                # Prefer enqueuing the reindex task so embedding performs the mark
                try:
                    from agriconnect.services.scraper.tasks import task_reindex_silver_document
                    task_reindex_silver_document.delay(file_hash=fh)
                except Exception:
                    try:
                        task_reindex_silver_document(file_hash=fh)
                    except Exception as e:
                        print(f"Failed to enqueue reindex for {fh}: {e}")
            else:
                # Synchronous fallback: call the embed task directly
                try:
                    from agriconnect.services.scraper.tasks import task_embed_chunks
                    task_embed_chunks(file_hash=fh)
                except Exception as e:
                    print(f"Failed to run embed for {fh}: {e}")
        print(f"Dispatched batch {page+1}/{pages}")

print("Backfill dispatch complete.")
