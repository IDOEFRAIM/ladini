"""Sync local backend/sources/raw_data -> S3 under <S3_BUCKET>/<S3_KEY_PREFIX>/raw_data/.

Usage: python sync_raw_to_s3.py [--force]
"""
from __future__ import annotations

import os
import sys
import json
from pathlib import Path
from argparse import ArgumentParser
import logging

try:
    import boto3
    from botocore.exceptions import ClientError
except Exception:
    boto3 = None
    ClientError = Exception


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sync_raw_to_s3")


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip()


def s3_key_for(local_path: Path, base_dir: Path, prefix: str) -> str:
    rel = local_path.relative_to(base_dir).as_posix()
    if prefix:
        return f"{prefix.rstrip('/')}/raw_data/{rel}"
    return f"raw_data/{rel}"


def main(argv=None) -> int:
    p = ArgumentParser()
    p.add_argument("--force", action="store_true", help="Upload and overwrite existing objects")
    args = p.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    env_path = root / ".env"
    load_env(env_path)

    s3_bucket = os.getenv("S3_BUCKET") or os.getenv("AWS_S3_BUCKET")
    s3_prefix = os.getenv("S3_KEY_PREFIX") or os.getenv("AWS_S3_PREFIX") or ""

    if not s3_bucket:
        logger.error("S3_BUCKET not set in environment or backend/.env")
        return 2

    if boto3 is None:
        logger.error("boto3 is not installed in the environment")
        return 3

    base_dir = root / "sources" / "raw_data"
    if not base_dir.exists():
        logger.warning(f"No raw_data directory found at {base_dir}")
        return 0

    s3 = boto3.client("s3", region_name=os.getenv("S3_REGION") or None)

    uploaded = 0
    skipped = 0
    errors = 0

    for local in sorted(base_dir.rglob("*")):
        if not local.is_file():
            continue
        key = s3_key_for(local, base_dir, s3_prefix)
        try:
            if not args.force:
                try:
                    s3.head_object(Bucket=s3_bucket, Key=key)
                    logger.info(f"Skip exists: s3://{s3_bucket}/{key}")
                    skipped += 1
                    continue
                except ClientError:
                    pass

            with open(local, "rb") as fh:
                s3.put_object(Bucket=s3_bucket, Key=key, Body=fh)
            logger.info(f"Uploaded: s3://{s3_bucket}/{key}")
            uploaded += 1
        except Exception as e:
            logger.exception("Failed upload %s: %s", local, e)
            errors += 1

    logger.info(f"Done. uploaded={uploaded} skipped={skipped} errors={errors}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
