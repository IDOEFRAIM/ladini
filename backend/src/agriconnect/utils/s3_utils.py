from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("agriconnect.s3_utils")

try:
    import boto3
    from botocore.exceptions import ClientError
except Exception:
    boto3 = None
    ClientError = Exception

from agriconnect.core.settings import settings


def upload_file_to_s3(local_path: str) -> Optional[str]:
    """Upload local file to configured S3 bucket under <prefix>/raw_data/...

    Returns the s3:// URI on success, or None on failure / not configured.
    """
    if boto3 is None:
        logger.debug("boto3 not installed; skipping S3 upload")
        return None
    s3_bucket = getattr(settings, "S3_BUCKET", "")
    if not s3_bucket:
        logger.debug("S3_BUCKET not configured; skipping S3 upload")
        return None

    try:
        base_dir = Path(settings.BASE_DIR).parent / "sources" / "raw_data"
    except Exception:
        base_dir = Path("backend") / "sources" / "raw_data"

    p = Path(local_path)
    try:
        rel = p.relative_to(base_dir).as_posix()
    except Exception:
        rel = p.name

    prefix = (getattr(settings, "S3_KEY_PREFIX", "") or "").rstrip('/')
    key = f"{prefix}/raw_data/{rel}" if prefix else f"raw_data/{rel}"

    try:
        s3 = boto3.client("s3", region_name=getattr(settings, "S3_REGION", None) or None)
        with open(p, "rb") as fh:
            s3.put_object(Bucket=s3_bucket, Key=key, Body=fh)
        uri = f"s3://{s3_bucket}/{key}"
        logger.info("Uploaded %s -> %s", p, uri)
        return uri
    except ClientError as e:
        logger.warning("S3 upload failed for %s: %s", p, e)
        return None
    except Exception as e:
        logger.exception("Unexpected error uploading %s: %s", p, e)
        return None
