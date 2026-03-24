"""
S3 Storage Abstraction.
Encapsulates Boto3 session management for consistency.
"""
import boto3
import json
import logging
from typing import Any, Optional
from datetime import datetime, timezone

from agriconnect.core.settings import settings
from backend.ingestion.schema import RawDocument

logger = logging.getLogger(__name__)

class S3Manager:
    """Handles raw data storage in S3."""

    def __init__(self, bucket_name: str = "agriconnect-raw", region: str = "eu-central-1"):
        # Ensure bucket is valid string
        buck = getattr(settings, "S3_BUCKET", bucket_name)
        self.bucket = buck if buck else bucket_name
        
        # Ensure region is valid string
        reg = getattr(settings, "S3_REGION", region)
        self.region = reg if reg else region
        self.client = self._create_client()

    def _create_client(self):
        """Creates a boto3 client with explicit settings."""
        # TODO: Add assume_role logic if needed in AWS Lambda/EC2 context
        return boto3.client(
            "s3",
            region_name=self.region,
            aws_access_key_id=getattr(settings, "AWS_ACCESS_KEY_ID", None),
            aws_secret_access_key=REDACTED_AWS_SECRET_ACCESS_KEY
        )

    def upload_raw_json(self, doc: RawDocument, prefix: str = "raws_data") -> str:
        """
        Uploads a RawDocument as JSON to S3.
        Returns the S3 key.
        """
        ts = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        safe_id = "".join([c if c.isalnum() else "_" for c in doc.source_id])
        key = f"{prefix}/{doc.source_type}/{ts}/{safe_id}.json"

        try:
            payload = doc.model_dump_json(exclude_none=True)
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=payload.encode("utf-8"),
                ContentType="application/json",
                Metadata={
                    "source_type": doc.source_type,
                    "ingested_at": doc.ingested_at.isoformat()
                }
            )
            logger.info("Uploaded to s3://%s/%s", self.bucket, key)
            return key
        except Exception as e:
            logger.error("Failed S3 upload for %s: %s", doc.source_id, e)
            raise

    def check_exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def move_to_failed(self, key: str, reason: str = "unknown") -> str:
        """
        Moves a file from its current location to the 'failed/' directory (DLQ).
        """
        try:
            # Construct new key in 'failed/' prefix, keeping the filename structure
            # keys usually look like: raws_data/source_type/YYYY/MM/DD/file.json
            # We want: failed/source_type/YYYY/MM/DD/file.json
            new_key = key.replace("raws_data/", "failed/", 1)
            if new_key == key: # If it didn't start with raws_data
                new_key = f"failed/{key}"

            # Copy object to new location
            copy_source = {'Bucket': self.bucket, 'Key': key}
            self.client.copy_object(
                CopySource=copy_source,
                Bucket=self.bucket,
                Key=new_key,
                MetadataDirective='REPLACE',
                Metadata={
                    "failure_reason": str(reason),
                    "failed_at": datetime.now(timezone.utc).isoformat()
                }
            )
            
            # Delete original object
            self.client.delete_object(Bucket=self.bucket, Key=key)
            
            logger.warning(f"Moved {key} to DLQ: {new_key} (Reason: {reason})")
            return new_key
        except Exception as e:
            logger.error(f"Failed to move {key} to DLQ: {e}")
            return None
