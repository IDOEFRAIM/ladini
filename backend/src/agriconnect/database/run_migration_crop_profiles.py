"""
Apply normalized crop profiles migration SQL.
Run as: python -m agriconnect.database.run_migration_crop_profiles
"""
from pathlib import Path
from sqlalchemy import text
from agriconnect.infrastructure.database.db import get_engine, resolve_database_url

SQL_PATH = Path(__file__).resolve().parent / "migration_crop_profiles_normalized.sql"


def main():
    db_url = resolve_database_url(required=True)
    engine = get_engine(db_url=db_url)
    sql = SQL_PATH.read_text(encoding="utf-8")
    print(f"Applying migration SQL from: {SQL_PATH}")
    # Split into pre-backfill and backfill sections to avoid failures when legacy table is absent
    marker = "-- Backfill crop_profiles from legacy JSONB payload in crop_knowledge."
    if marker in sql:
        pre, backfill = sql.split(marker, 1)
        pre_sql = pre
        backfill_sql = marker + backfill
    else:
        pre_sql = sql
        backfill_sql = ""

    with engine.begin() as conn:
        # Execute pre-backfill statements
        for stmt in pre_sql.split(";\n"):
            s = stmt.strip()
            if not s:
                continue
            try:
                conn.execute(text(s))
            except Exception as e:
                print(f"Error executing statement (pre-backfill): {e}")
                raise

        # Execute backfill only if legacy table exists
        if backfill_sql:
            exists = conn.execute(text("SELECT to_regclass('public.crop_knowledge')")).scalar()
            if exists:
                print("Legacy table crop_knowledge found — running backfill.")
                for stmt in backfill_sql.split(";\n"):
                    s = stmt.strip()
                    if not s:
                        continue
                    try:
                        conn.execute(text(s))
                    except Exception as e:
                        print(f"Error executing statement (backfill): {e}")
                        raise
            else:
                print("Legacy table crop_knowledge not found — skipping backfill.")

    print("Migration applied (statements executed or skipped backfill).")


if __name__ == "__main__":
    main()
