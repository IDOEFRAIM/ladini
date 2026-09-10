Run local ingestion (testing)
=============================

Purpose
-------
Small helper to start the project's ingestion worker locally for testing.

Important
---------
- This script disables `robots.txt` checks by setting `SCRAPER_IGNORE_ROBOTS=1`.
- Use only for local testing and debugging. Do not use against third-party sites without permission.

Usage
-----
From the repository root and with your virtualenv active:

```bash
python scripts/run_full_ingestion.py --max-files 20
```

Options
-------
- `--continuous`: run in continuous loop
- `--max-files`: limit number of S3 objects processed (0 = all)
- `--s3-prefix-filter`: filter S3 keys by prefix
- `--ignore-robots`: explicitly set `SCRAPER_IGNORE_ROBOTS=1` (the script sets it by default)

Preconditions
-------------
- Ensure `DATABASE_URL` (Postgres) and `S3_BUCKET` (or local S3-compatible settings) are configured in env.
- Ensure `REDIS_URL` if your VectorDB uses Redis.

Notes
-----
The script will log environment hints and will raise descriptive errors if required services are missing.
