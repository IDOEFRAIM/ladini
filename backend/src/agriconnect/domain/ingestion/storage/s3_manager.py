"""
S3 Storage Abstraction.
Encapsulates Boto3 session management for consistency.
"""
import boto3
import logging
import os
import json
import hashlib
from urllib.parse import urlparse
from datetime import datetime, timezone

from agriconnect.core.settings import settings
from agriconnect.core.schemas import RawDocument

logger = logging.getLogger(__name__)

RAW_DOCUMENT_SCHEMA_VERSION = 1

class S3Manager:
    """Handles raw data storage in S3."""

    def __init__(self, bucket_name: str = "agriconnect-raw", region: str = "eu-central-1"):
        # Ensure bucket is valid string
        buck = getattr(settings, "S3_BUCKET", bucket_name)
        self.bucket = buck if buck else bucket_name
        
        # Region resolution order: S3_REGION -> AWS_REGION -> env AWS_REGION -> fallback
        reg = (
            getattr(settings, "S3_REGION", "")
            or getattr(settings, "AWS_REGION", "")
            or os.getenv("AWS_REGION", "")
            or region
        )
        self.region = reg if reg else region
        self.client = self._create_client()

    def _create_client(self):
        """Creates a boto3 client with explicit settings."""
        # TODO: Add assume_role logic if needed in AWS Lambda/EC2 context
        access_key = getattr(settings, "AWS_ACCESS_KEY_ID", "") or os.getenv("AWS_ACCESS_KEY_ID", "")
        secret_key = getattr(settings, "AWS_SECRET_ACCESS_KEY", "") or os.getenv("AWS_SECRET_ACCESS_KEY", "")
        session_token = getattr(settings, "AWS_SESSION_TOKEN", "") or os.getenv("AWS_SESSION_TOKEN", "")

        kwargs = {"region_name": self.region}
        # Let boto3 default provider chain handle auth when explicit keys are absent.
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key
            if session_token:
                kwargs["aws_session_token"] = session_token

        return boto3.client(
            "s3",
            **kwargs,
        )

    def upload_raw_json(self, doc: RawDocument, prefix: str = "raws_data") -> str:
        """
        Uploads a RawDocument as JSON to S3.
        Returns the S3 key.
        """
        ts = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        source_id = str((doc.metadata or {}).get("source_id") or doc.id)
        source_type = str((doc.metadata or {}).get("source_type") or "scraped_document")
        safe_id = "".join([c if c.isalnum() else "_" for c in source_id])
        key = f"{prefix}/{source_type}/{ts}/{safe_id}.json"

        try:
            payload_dict = {
                "schema_version": RAW_DOCUMENT_SCHEMA_VERSION,
                "payload_type": "RawDocument",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "raw_document": doc.model_dump(exclude_none=True),
            }
            payload = json.dumps(payload_dict, ensure_ascii=False)
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=payload.encode("utf-8"),
                ContentType="application/json",
                Metadata={
                    "source_type": source_type,
                    "schema_version": str(RAW_DOCUMENT_SCHEMA_VERSION),
                    "ingested_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            logger.info("Uploaded to s3://%s/%s", self.bucket, key)
            return key
        except Exception as e:
            logger.error("Failed S3 upload for %s: %s", source_id, e)
            raise

    def save_raw(self, doc: RawDocument, prefix: str = "raws_data") -> str:
        """Public ingestion API: persist one streamed RawDocument immediately."""
        return self.upload_raw_json(doc=doc, prefix=prefix)

    def upload_pdf_binary(self, payload: bytes, pdf_url: str, source_id: str, prefix: str = "raws_data") -> str:
        """Upload a validated PDF binary to S3 and return the object key."""
        ts = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        parsed = urlparse(pdf_url or "")
        filename = os.path.basename(parsed.path or "") or "document.pdf"
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"
        safe_source = "".join([c if c.isalnum() else "_" for c in (source_id or "unknown_source")])
        digest = hashlib.sha256(payload or b"").hexdigest()[:16]
        key = f"{prefix}/pdf/{safe_source}/{ts}/{digest}_{filename}"
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=payload,
            ContentType="application/pdf",
            Metadata={
                "source_id": safe_source,
                "pdf_url": (pdf_url or "")[:1024],
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        logger.info("Uploaded PDF to s3://%s/%s", self.bucket, key)
        return key

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

    def move_to_archive(self, key: str, archive_prefix: str = "archive/", reason: str = "archived_by_hygiene") -> str:
        """
        Move a file to an archive prefix to keep raw ingestion zone clean.
        Returns the new key or None on failure.
        """
        try:
            # Prefer to preserve directory structure where possible
            new_key = key.replace("raws_data/", f"{archive_prefix}", 1)
            if new_key == key:
                new_key = f"{archive_prefix.rstrip('/')}/{key}"

            copy_source = {"Bucket": self.bucket, "Key": key}
            self.client.copy_object(
                CopySource=copy_source,
                Bucket=self.bucket,
                Key=new_key,
                MetadataDirective='REPLACE',
                Metadata={
                    "archived_reason": str(reason),
                    "archived_at": datetime.now(timezone.utc).isoformat(),
                },
            )

            # Delete original object
            self.client.delete_object(Bucket=self.bucket, Key=key)
            logger.info("Archived %s to s3://%s/%s (Reason: %s)", key, self.bucket, new_key, reason)
            return new_key
        except Exception as e:
            logger.error("Failed to archive %s to %s: %s", key, archive_prefix, e)
            return None

    def save_audit_report(self, report: dict, prefix: str = None) -> str:
        """
        Save an ingestion audit report (JSON) to S3 under the configured audit prefix.
        Returns the S3 key of the saved report.
        """
        try:
            from agriconnect.core.settings import settings
            audit_prefix = (prefix or getattr(settings, "INGESTION_AUDIT_PREFIX", "ingestion_audit")).strip("/")
        except Exception:
            audit_prefix = (prefix or "ingestion_audit").strip("/")

        key = f"{audit_prefix}/report_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        try:
            payload = json.dumps(report, ensure_ascii=False).encode("utf-8")
            self.client.put_object(Bucket=self.bucket, Key=key, Body=payload, ContentType="application/json")
            logger.info("Saved audit report to s3://%s/%s", self.bucket, key)
            return key
        except Exception as e:
            logger.error("Failed to save audit report to S3: %s", e)
            return ""
