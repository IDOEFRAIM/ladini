#!/usr/bin/env python3
"""Clean existing weather advisory JSON files in S3 by sanitizing bulletin_excerpt.

Usage: set `S3_BUCKET`, optionally `AWS_REGION` and AWS creds in env, then run:
  python backend/scripts/clean_weather_advisories.py
"""
import os
import json
import logging
from typing import Optional

import boto3

from agriconnect.services.data_collection.weather.weather_collector import WeatherFetcher, LocationService


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("clean_weather_advisories")


def get_s3_client(region: Optional[str] = None):
    if region:
        return boto3.client("s3", region_name=region)
    return boto3.client("s3")


def main():
    bucket = os.getenv("S3_BUCKET")
    if not bucket:
        raise SystemExit("S3_BUCKET must be set in the environment")

    region = os.getenv("AWS_REGION")
    prefix = os.getenv("S3_RAW_PREFIX", "raw_data/weather_advisories").strip("/")

    s3 = get_s3_client(region)
    wf = WeatherFetcher(LocationService())

    paginator = s3.get_paginator("list_objects_v2")
    total = 0
    updated = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj.get("Key")
            if not key or key.endswith("/"):
                continue
            total += 1
            try:
                resp = s3.get_object(Bucket=bucket, Key=key)
                body = resp["Body"].read()
                data = json.loads(body.decode("utf-8"))
            except Exception as e:
                logger.warning("Failed to fetch/parse %s: %s", key, e)
                continue

            meta = data.get("metadata") or {}
            old = meta.get("bulletin_excerpt", "")
            new = wf._sanitize_bulletin_text(old)
            if new != (old or ""):
                meta["bulletin_excerpt"] = new
                data["metadata"] = meta
                try:
                    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(data, ensure_ascii=False).encode("utf-8"), ContentType="application/json")
                    updated += 1
                    logger.info("Updated: %s", key)
                except Exception as e:
                    logger.error("Failed to write %s: %s", key, e)

    logger.info("Done. Scanned=%s, Updated=%s", total, updated)


if __name__ == "__main__":
    main()
