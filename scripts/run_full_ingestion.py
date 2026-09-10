#!/usr/bin/env python3
"""Lance l'ingestion complète en mode test.

Ce script active le contournement de `robots.txt` via `SCRAPER_IGNORE_ROBOTS=1`
et démarre l'IngestionWorker. Usage local pour tests uniquement.

Attention: n'utilisez ce script QUE pour des tests locaux ; respecter robots.txt
et la loi pour tout scraping de production.
"""
import os
import argparse
import logging

logger = logging.getLogger("run_full_ingestion")
logging.basicConfig(level=logging.INFO)


def main():
    parser = argparse.ArgumentParser(description="Run full ingestion (testing).")
    parser.add_argument("--continuous", action="store_true", help="Run worker in continuous mode")
    parser.add_argument("--max-files", type=int, default=0, help="Max files to process this run (0 = all)")
    parser.add_argument("--s3-prefix-filter", default="", help="Optional S3 prefix filter to limit ingestion scope")
    parser.add_argument("--ignore-robots", action="store_true", help="Also set SCRAPER_IGNORE_ROBOTS=1 (default true for this script)")
    args = parser.parse_args()

    # Dev/testing: bypass robots.txt checks when requested (or by default here)
    if args.ignore_robots or os.getenv("SCRAPER_IGNORE_ROBOTS", "") == "":
        os.environ["SCRAPER_IGNORE_ROBOTS"] = "1"
        logger.info("SCRAPER_IGNORE_ROBOTS=1 (robots.txt checks disabled for this run)")

    # Helpful debug: show critical envs the worker depends on
    env_hints = {
        "DATABASE_URL": os.getenv("DATABASE_URL") or os.getenv("PGDATABASE"),
        "S3_BUCKET": os.getenv("S3_BUCKET"),
        "REDIS_URL": os.getenv("REDIS_URL") or os.getenv("AGRICONNECT_REDIS_URL"),
    }
    logger.info("Environment hints: %s", {k: bool(v) for k, v in env_hints.items()})

    try:
        # Import here to fail with a clear message if project not on PYTHONPATH
        from agriconnect.domain.ingestion.worker import IngestionWorker

        worker = IngestionWorker(raw_data_dir="/tmp/ingestion_raw", s3_prefix_filter=args.s3_prefix_filter, max_files=args.max_files)
        # Run in scraper stream mode which pulls scrapers and ingests one-by-one.
        if args.continuous:
            worker.run(continuous=True)
        else:
            worker.run(continuous=False)

    except Exception as exc:
        logger.exception("Failed to start ingestion worker: %s", exc)
        raise


if __name__ == "__main__":
    main()
