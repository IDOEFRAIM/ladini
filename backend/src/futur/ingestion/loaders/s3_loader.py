"""S3 loader for ingestion consumer mode.

The loader only reads JSON payloads already persisted in S3 raw zone.
"""

from __future__ import annotations

import json
import hashlib
import os
from typing import Any, Dict, Iterable, List, Optional, Set
import boto3

from futur.ingestion.storage.s3_manager import S3Manager
from agriconnect.core.settings import settings
import logging

logger = logging.getLogger(__name__)


class S3Loader:
    """Load raw objects from S3 for replay/reprocessing workflows."""

    def __init__(self, s3_manager: Optional[S3Manager] = None, bucket_name: Optional[str] = None):
        resolved_bucket = (
            bucket_name
            or os.getenv("INGESTION_S3_BUCKET")
            or os.getenv("S3_BUCKET")
            or getattr(settings, "S3_BUCKET", "")
            or "agriconnect-raw"
        )
        region_name = (
            os.getenv("S3_REGION")
            or os.getenv("AWS_REGION")
            or getattr(settings, "S3_REGION", "")
            or getattr(settings, "AWS_REGION", "")
            or "eu-central-1"
        )

        if s3_manager is None:
            self.s3 = S3Manager(bucket_name=resolved_bucket, region=region_name)
        else:
            self.s3 = s3_manager
            # Allow explicit env override even when a manager is injected.
            self.s3.bucket = resolved_bucket

        # Explicit boto3 client reference for stream/event-driven consumers.
        self.client = boto3.client("s3", region_name=region_name)

    @staticmethod
    def build_marker(bucket: str, key: str, etag: Optional[str]) -> str:
        suffix = (etag or "noetag").strip('"')
        return f"s3://{bucket}/{key}#etag={suffix}"

    def list_raw_objects(
        self,
        prefixes: Iterable[str],
        processed_markers: Optional[Set[str]] = None,
        max_files: int = 0,
    ) -> List[Dict[str, Any]]:
        processed = processed_markers or set()
        seen_markers: Set[str] = set()
        objects: List[Dict[str, Any]] = []

        # Normalize exclude prefixes from settings (can be list or comma-separated string)
        raw_excludes = getattr(settings, "INGESTION_S3_EXCLUDE_PREFIXES", []) or []
        if isinstance(raw_excludes, str):
            exclude_prefixes = [p.strip().lower() for p in raw_excludes.split(",") if p.strip()]
        else:
            exclude_prefixes = [p.strip().lower() for p in raw_excludes]

        for prefix in prefixes:
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.s3.bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    key = obj.get("Key")
                    if not key or key.endswith("/") or not key.lower().endswith(".json"):
                        continue
                    # Exclude keys that match configured snapshot prefixes to avoid noisy DLQs
                    lower_key = key.lower()
                    skip = False
                    for ex in exclude_prefixes:
                        if not ex:
                            continue
                        # match either at start or as a path segment
                        if lower_key.startswith(f"{ex}/") or f"/{ex}/" in lower_key:
                            try:
                                archived = self.s3.move_to_archive(key, reason="excluded_by_ingestion_hygiene")
                                logger.info("Archived excluded key %s -> %s", key, archived)
                            except Exception as e:
                                logger.warning("Failed to archive excluded key %s: %s", key, e)
                            skip = True
                            break
                    if skip:
                        continue

                    # Archive obvious metadata files by name pattern to reduce noise
                    if "/metadata/" in lower_key or lower_key.endswith("_meta.json") or "/pdfs/metadata/" in lower_key:
                        try:
                            archived = self.s3.move_to_archive(key, reason="excluded_by_name_metadata")
                            logger.info("Archived metadata key %s -> %s", key, archived)
                        except Exception as e:
                            logger.warning("Failed to archive metadata key %s: %s", key, e)
                        continue

                    # Quick content-based heuristic: fetch a small JSON prefix to detect crawl snapshots / metadata files
                    try:
                        snippet = self.client.get_object(Bucket=self.s3.bucket, Key=key, Range='bytes=0-8192')["Body"].read()
                        try:
                            sample_obj = json.loads(snippet.decode('utf-8', errors='replace'))
                        except Exception:
                            sample_obj = None

                        if isinstance(sample_obj, dict):
                            top_keys = set(sample_obj.keys())
                            # Common snapshot indicators: 'scraped_at', 'report', 'errors', or files that primarily contain 'metadata' blocks
                            if top_keys & {"scraped_at", "report", "errors"}:
                                try:
                                    archived = self.s3.move_to_archive(key, reason="excluded_by_content_snapshot")
                                    logger.info("Archived snapshot-like key %s -> %s", key, archived)
                                except Exception as e:
                                    logger.warning("Failed to archive snapshot-like key %s: %s", key, e)
                                continue
                            if "metadata" in top_keys and len(top_keys) <= 5:
                                try:
                                    archived = self.s3.move_to_archive(key, reason="excluded_by_content_metadata")
                                    logger.info("Archived metadata-like key %s -> %s", key, archived)
                                except Exception as e:
                                    logger.warning("Failed to archive metadata-like key %s: %s", key, e)
                                continue
                    except Exception:
                        # Range GET failed or non-JSON content; fall back to name-based heuristics
                        pass

                    etag = str(obj.get("ETag") or "").strip('"')
                    marker = self.build_marker(self.s3.bucket, key, etag)
                    if marker in processed or marker in seen_markers:
                        continue

                    seen_markers.add(marker)
                    objects.append(
                        {
                            "bucket": self.s3.bucket,
                            "key": key,
                            "marker": marker,
                            "etag": etag,
                            "last_modified": obj.get("LastModified"),
                        }
                    )
                    if max_files > 0 and len(objects) >= max_files:
                        return objects

        return objects

    def load_json(self, key: str) -> Dict[str, Any]:
        body = self.client.get_object(Bucket=self.s3.bucket, Key=key)["Body"].read()
        payload = json.loads(body.decode("utf-8", errors="replace"))

        # New envelope format (schema versioned)
        if isinstance(payload, dict) and "schema_version" in payload:
            schema_version = int(payload.get("schema_version") or 0)
            if schema_version == 1 and isinstance(payload.get("raw_document"), dict):
                raw_doc = dict(payload["raw_document"])
                raw_doc.setdefault("metadata", {})
                raw_doc["metadata"]["_raw_schema_version"] = schema_version
                return raw_doc
            raise ValueError(f"Unsupported raw payload schema_version={schema_version} for key={key}")

        # Legacy fallback: object is directly the RawDocument JSON payload.
        if isinstance(payload, dict):
            # map legacy v0 to v1 explicitly via migrator
            try:
                migrated = self.map_v0_to_v1(payload)
                migrated.setdefault("metadata", {})
                migrated["metadata"]["_raw_schema_version"] = 0
                return migrated
            except Exception:
                raise ValueError(f"Legacy payload mapping failed for key={key}")

        raise ValueError(f"Unexpected payload format for key={key}")

    def map_v0_to_v1(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Migrate legacy RawDocument payload (v0) to the v1 RawDocument dict shape.

        This function should be expanded to handle historical schema shapes. For
        now, handle the simple case where the legacy object already resembles
        RawDocument or contains keys like 'text' or 'body'.
        """
        # If payload already looks like RawDocument, return as-is (shallow copy)
        if "id" in payload and "content_markdown" in payload:
            return dict(payload)

        # Common legacy shapes: {'url':..., 'text': '...', 'title':...}
        # textual fields
        for txt_field in ("content", "content_markdown", "text", "body"):
            if txt_field in payload and isinstance(payload.get(txt_field), str) and payload.get(txt_field).strip():
                text_val = payload.get(txt_field)
                metadata = payload.get("metadata") or {}
                # attempt to preserve any declared source_type
                if not metadata.get("source_type"):
                    # heuristics: news-like vs weather vs technical
                    if payload.get("source_type"):
                        metadata["source_type"] = payload.get("source_type")
                    elif "news" in (payload.get("source_type") or "") or payload.get("url", "").lower().find("/news/") >= 0:
                        metadata["source_type"] = "news_article"
                    elif "meteo" in (payload.get("url") or "") or "pdf_url" in payload:
                        metadata["source_type"] = "weather_bulletin"
                    else:
                        metadata["source_type"] = "technical_resource"

                return {
                    "id": payload.get("id") or hashlib.sha256(text_val.encode("utf-8")).hexdigest(),
                    "url": payload.get("url") or payload.get("source_url") or payload.get("file_path") or "",
                    "title": payload.get("title") or payload.get("filename") or "",
                    "content_markdown": text_val,
                    "metadata": metadata,
                }

        # Google Forms / survey-like payloads: synthesize a simple markdown from title+description+questions
        if "questions" in payload and isinstance(payload.get("questions"), (list, tuple)):
            parts = []
            if payload.get("title"):
                parts.append(f"# {payload.get('title')}")
            if payload.get("description"):
                parts.append(payload.get("description"))
            for q in payload.get("questions"):
                try:
                    qtext = q.get("label") or q.get("question") or str(q)
                except Exception:
                    qtext = str(q)
                parts.append(f"- {qtext}")
            md = "\n\n".join(parts)
            metadata = payload.get("metadata") or {}
            metadata.setdefault("source_type", "technical_resource")
            return {
                "id": payload.get("id") or hashlib.sha256(md.encode("utf-8")).hexdigest(),
                "url": payload.get("url") or "",
                "title": payload.get("title") or "Google Form",
                "content_markdown": md,
                "metadata": metadata,
            }

        # Meteo / batch result shape: {'status': 'SUCCESS', 'results': [{pdf_url/local_path, title, date}, ...]}
        if "status" in payload and isinstance(payload.get("results"), (list, tuple)):
            parts = []
            parts.append(f"# Batch: {payload.get('title') or payload.get('source') or 'meteos'}")
            for r in payload.get("results"):
                try:
                    title = r.get("title") or r.get("date") or r.get("pdf_url") or r.get("local_path") or "item"
                except Exception:
                    title = "item"
                link = r.get("pdf_url") or r.get("detail_url") or r.get("local_path") or ""
                if link:
                    parts.append(f"- [{title}]({link})")
                else:
                    parts.append(f"- {title}")
            md = "\n\n".join(parts)
            metadata = payload.get("metadata") or {}
            metadata.setdefault("source_type", "weather_bulletin")
            return {
                "id": payload.get("id") or hashlib.sha256(md.encode("utf-8")).hexdigest(),
                "url": payload.get("url") or "",
                "title": payload.get("title") or "meteo_batch",
                "content_markdown": md,
                "metadata": metadata,
            }

        # PDF metadata-only blobs: preserve metadata, produce empty content to mark as processed
        if ("filename" in payload or "file_path" in payload) and "extracted_chars" in payload:
            title = payload.get("title") or payload.get("filename") or payload.get("file_path") or "pdf_metadata"
            md = ""  # No textual content in metadata-only extract
            metadata = payload.get("metadata") or {}
            metadata.setdefault("source_type", "technical_resource")
            metadata.update({k: payload.get(k) for k in ("filename", "file_path", "pages", "extracted_chars") if k in payload})
            return {
                "id": payload.get("id") or hashlib.sha256(str(title).encode("utf-8")).hexdigest(),
                "url": payload.get("file_path") or payload.get("url") or "",
                "title": title,
                "content_markdown": md,
                "metadata": metadata,
            }
        # If none matched, fail explicitly so the Worker can DLQ the object.
        raise ValueError("Unable to map legacy payload to RawDocument v1")
