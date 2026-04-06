"""
Check DB tables vs SQLAlchemy models (models_v3.py).
Run with PYTHONPATH=backend/src python .../check_schema_alignment.py
"""
import sys
from sqlalchemy import text

from agriconnect.core.db import get_engine, resolve_database_url

# Import models and get SQLAlchemy Base metadata
from agriconnect.services import models_v3 as models

EXPECTED_TABLES = set(t.name for t in models.Base.metadata.sorted_tables)

DB_SCHEMAS = ["auth", "governance", "marketplace", "intelligence"]


def get_db_tables(conn):
    rows = conn.execute(
        text(
            """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema = ANY(:schemas)
            ORDER BY table_schema, table_name
            """
        ),
        {"schemas": DB_SCHEMAS},
    ).mappings().all()
    return set(f"{r['table_schema']}.{r['table_name']}" for r in rows)


def normalize_expected(table_name):
    # models table.name may include schema if reflected; ensure schema prefix
    # Our models use schema via __table_args__ in models_v3; SQLAlchemy table key format is schema.name
    return table_name


def main():
    db_url = resolve_database_url(required=True)
    engine = get_engine(db_url)
    with engine.connect() as conn:
        db_tables = get_db_tables(conn)

    # Build expected set with schema prefix if present in Table object
    expected = set()
    for table in models.Base.metadata.sorted_tables:
        if table.schema:
            expected.add(f"{table.schema}.{table.name}")
        else:
            expected.add(table.name)

    print("\nExpected tables (from models_v3):")
    for t in sorted(expected):
        print("  ", t)

    print("\nTables present in DB:")
    for t in sorted(db_tables):
        print("  ", t)

    missing = expected - db_tables
    extra = db_tables - expected

    print("\nSummary:")
    print(f"  Expected: {len(expected)} tables")
    print(f"  Present: {len(db_tables)} tables")
    print(f"  Missing: {len(missing)}")
    print(f"  Extra: {len(extra)}")

    if missing:
        print("\nMissing tables:")
        for t in sorted(missing):
            print("  -", t)
    else:
        print("\nNo missing tables: models aligned with DB tables (schemas checked).")

    if extra:
        print("\nExtra tables in DB (not in models):")
        for t in sorted(extra):
            print("  -", t)


if __name__ == '__main__':
    main()
