"""
Safe production flush script.
- Truncates Postgres tables `chunks` and `processed_documents` using TRUNCATE ... CASCADE
- Flushes Redis (`flushall()`)
- Requires explicit confirmation before running

Usage:
  Set `DATABASE_URL` and `REDIS_URL` environment variables, then run:
    python scripts/flush_production.py

If `DATABASE_URL` is not set, the script will prompt for DB credentials to build
`postgresql://user:password@localhost:5433/dbname`.
"""

import os
import sys
import logging
from getpass import getpass
from sqlalchemy import create_engine, text
import redis

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("flush_production")

TARGET_TABLES = ["chunks", "processed_documents"]


def get_database_url():
    url = os.getenv("DATABASE_URL")
    if url:
        return url

    # If not set in the environment, try to read backend/.env to find DATABASE_URL
    try:
        env_path = os.path.join(os.path.dirname(__file__), "..", "backend", ".env")
        env_path = os.path.abspath(env_path)
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("DATABASE_URL="):
                        _, val = line.split("=", 1)
                        candidate = val.strip().strip('"')
                        if candidate:
                            return candidate
    except Exception:
        pass

    print("DATABASE_URL not set. Please provide DB credentials to build the URL:")
    user = input("DB user: ").strip()
    password = getpass("DB password: ")
    dbname = input("DB name: ").strip()
    if not (user and password and dbname):
        logger.error("Missing DB credentials; aborting.")
        sys.exit(2)
    return f"postgresql://{user}:{password}@localhost:5433/{dbname}"


def confirm():
    # Allow automated runs when FORCE_FLUSH=1 is set in the environment
    if os.getenv("FORCE_FLUSH") == "1":
        logger.info("FORCE_FLUSH=1 detected; skipping interactive confirmation.")
        return

    ans = input("Êtes-vous sûr de vouloir VIDER LA PRODUCTION ? (y/n) ").strip().lower()
    if ans != "y":
        print("Annulation par l'utilisateur. Aucune action effectuée.")
        sys.exit(0)


def truncate_tables(engine):
    try:
        with engine.connect() as conn:
            # show counts before
            for t in TARGET_TABLES:
                try:
                    cnt = conn.execute(text(f"SELECT COUNT(*) FROM {t}"))
                    before = cnt.scalar()
                except Exception:
                    before = "n/a"
                logger.info("Table %s: %s rows before purge", t, before)

            logger.info("Executing TRUNCATE ... CASCADE on %s", ",".join(TARGET_TABLES))
            with conn.begin() as trans:
                conn.execute(text(f"TRUNCATE TABLE {', '.join(TARGET_TABLES)} CASCADE;"))
                trans.commit()

            # show counts after
            for t in TARGET_TABLES:
                try:
                    cnt = conn.execute(text(f"SELECT COUNT(*) FROM {t}"))
                    after = cnt.scalar()
                except Exception:
                    after = "n/a"
                logger.info("Table %s: %s rows after purge", t, after)

        logger.info("Postgres truncate completed successfully.")
    except Exception as e:
        logger.exception("Postgres truncate failed: %s", e)
        raise


def flush_redis():
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6380")
    try:
        r = redis.from_url(redis_url, decode_responses=True)
        try:
            size_before = r.dbsize()
        except Exception:
            size_before = "n/a"
        logger.info("Redis (%s) size before: %s", redis_url, size_before)

        # sample a few keys (non-blocking)
        try:
            sample = []
            it = r.scan_iter(match="*", count=10)
            for i, k in enumerate(it):
                sample.append(k)
                if i >= 9:
                    break
            logger.info("Redis sample keys (up to 10): %s", sample)
        except Exception:
            logger.info("Could not sample Redis keys")

        r.flushall()
        try:
            size_after = r.dbsize()
        except Exception:
            size_after = "n/a"
        logger.info("Redis size after flushall: %s", size_after)
        logger.info("Redis flushall succeeded.")
    except Exception as e:
        logger.exception("Redis flush failed: %s", e)
        raise


def main():
    confirm()

    db_url = get_database_url()
    engine = create_engine(db_url, pool_pre_ping=True)

    try:
        truncate_tables(engine)
    except Exception:
        logger.error("Aborting due to Postgres error.")
        sys.exit(3)

    try:
        flush_redis()
    except Exception:
        logger.error("Redis flush encountered an error.")
        sys.exit(4)

    logger.info("Production flush completed successfully.")


if __name__ == "__main__":
    main()
