#!/usr/bin/env python3
"""Manual verification helper for UNIQUE(content_hash) lock.

Prints whether UNIQUE(content_hash) exists on the selected chunks table.
This is intended to mirror the mandatory manual psql verification.
"""

import os
import sys
from sqlalchemy import create_engine, text


def main() -> int:
    db_url = os.getenv("DATABASE_URL") or os.getenv("AGRICONNECT_DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    table = (os.getenv("AGRICONNECT_CHUNKS_TABLE") or "ingestion.document_chunks").strip()
    if "." not in table:
        print(f"Invalid table name: {table}", file=sys.stderr)
        return 2
    schema_name, table_name = table.split(".", 1)

    engine = create_engine(db_url)
    q = text(
        """
        SELECT c.conname
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN unnest(c.conkey) AS ck(attnum) ON true
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ck.attnum
        WHERE c.contype = 'u'
          AND n.nspname = :schema_name
          AND t.relname = :table_name
          AND a.attname = 'content_hash'
        LIMIT 1
        """
    )

    with engine.connect() as conn:
        row = conn.execute(q, {"schema_name": schema_name, "table_name": table_name}).first()

    if not row:
        print(f"FAIL: UNIQUE(content_hash) missing on {table}")
        return 1

    print(f"OK: UNIQUE(content_hash) present on {table} (constraint={row[0]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
