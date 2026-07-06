from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
import os

SQL_PATH = os.path.join(os.path.dirname(__file__), "seed_inera_standard.sql")

def _get_sync_engine():
    # Use DATABASE_URL from env; fallback to default in project config
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        # Allow project-local configuration via AGRICONNECT_DATABASE_URL
        database_url = os.environ.get("AGRICONNECT_DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL not set; cannot run seed script")
    return create_engine(database_url)

def main():
    with open(SQL_PATH, "r", encoding="utf-8") as fh:
        sql = fh.read()

    engine = _get_sync_engine()
    print(f"Applying seed SQL from: {SQL_PATH}")
    with engine.begin() as conn:
        for stmt in sql.split(";\n"):
            s = stmt.strip()
            if not s:
                continue
            conn.execute(text(s))

    print("Seed applied.")

if __name__ == '__main__':
    main()
