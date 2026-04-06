"""
Event-Driven Ingestion Worker.
Monitors configured paths/S3 prefixes for new files and processes them.
"""

import os
import time
import logging
import io
import json
import hashlib
from pathlib import Path
from typing import List, Optional, Union

from tqdm import tqdm
from sqlalchemy import text

from agriconnect.rag.components import get_embedding_model
from agriconnect.core.db import get_engine, resolve_database_url
from agriconnect.core.schemas import RawDocument

from agriconnect.domain.ingestion.processors.factory import get_processor, get_processor_for_document
from agriconnect.domain.ingestion.storage.vector_db import VectorDBManager
from agriconnect.domain.ingestion.storage.s3_manager import S3Manager
from agriconnect.domain.ingestion.loaders.s3_loader import S3Loader
from agriconnect.services.scraper.scraper_orchestrator import ScraperOrchestrator, SourceRunResult
from agriconnect.core.settings import settings

logger = logging.getLogger("ingestion_worker")
logging.basicConfig(level=logging.INFO)

class IngestionWorker:
    """
    Orchestrates the ingestion pipeline:
    1. Detects Source (Directory/S3)
    2. Routes to Processor (PDF/JSON/News)
    3. Cleans & Chunks
    4. Embeds
    5. Stores in VectorDB (Redis)
    """

    def __init__(self, raw_data_dir: str, s3_prefix_filter: str = "", max_files: int = 0):
        self.raw_data_dir = Path(raw_data_dir)
        self.vector_db = VectorDBManager()
        self.embedding_model = get_embedding_model() # LlamaIndex EmbedModel
        self.s3_manager: Optional[S3Manager] = None
        self.s3_loader: Optional[S3Loader] = None
        self.max_files = max(0, int(max_files or 0))
        self.db_url = resolve_database_url(required=False)
        self.state_engine = None
        self._state_in_memory: set[str] = set()
        if self.db_url:
            try:
                self.state_engine = get_engine(self.db_url)
            except Exception as exc:
                logger.warning(
                    "State DB unavailable; using in-memory processed markers for this run only: %s",
                    exc,
                )
        else:
            logger.warning("No DATABASE_URL resolved; using in-memory processed markers for this run only")
        self.s3_prefix = (getattr(settings, "S3_KEY_PREFIX", "") or "").strip("/")
        self.s3_raw_prefix = os.getenv("S3_RAW_PREFIX", "raw_data").strip("/")
        self.s3_prefix_filter = (s3_prefix_filter or "").strip("/")
        # Cloud-only: no local ingestion fallback.
        self.source_mode = (os.getenv("INGESTION_SOURCE", "s3") or "s3").lower()
        if self.source_mode not in {"auto", "s3"}:
            logger.warning("Unknown INGESTION_SOURCE=%s, falling back to auto", self.source_mode)
            self.source_mode = "auto"

        if self.source_mode == "local":
            raise RuntimeError("INGESTION_SOURCE=local is forbidden in cloud mode. Use s3.")

        if self._should_use_s3():
            try:
                self.s3_manager = S3Manager()
                self.s3_loader = S3Loader(self.s3_manager)
                # Early connectivity check
                self.s3_manager.client.head_bucket(Bucket=self.s3_manager.bucket)
                logger.info("S3 ingestion enabled on bucket=%s", self.s3_manager.bucket)
            except Exception as e:
                raise RuntimeError(f"S3 ingestion required but unavailable: {e}") from e

        if not self.s3_manager:
            raise RuntimeError("S3 manager not initialized. Ingestion worker runs in S3-only mode.")

    def _compute_file_hash(self, body: bytes) -> str:
        return hashlib.md5(body).hexdigest()

    def _is_document_unchanged(self, s3_key: str, file_hash: str) -> bool:
        if self.state_engine is None:
            return False
        query = text(
            """
            SELECT 1
            FROM ingestion.ingested_documents
            WHERE s3_key = :s3_key
              AND file_hash = :file_hash
              AND status = 'processed'
            LIMIT 1
            """
        )
        with self.state_engine.connect() as conn:
            row = conn.execute(query, {"s3_key": s3_key, "file_hash": file_hash}).first()
        return row is not None

    def _upsert_ingested_document(
        self,
        *,
        s3_key: str,
        file_hash: str,
        chunk_count: int,
        status: str,
        metadata: dict,
    ) -> None:
        if self.state_engine is None:
            return
        query = text(
            """
            INSERT INTO ingestion.ingested_documents (s3_key, file_hash, status, chunk_count, last_processed_at, metadata)
            VALUES (:s3_key, :file_hash, :status, :chunk_count, NOW(), CAST(:metadata AS JSONB))
            ON CONFLICT (s3_key)
            DO UPDATE SET
                file_hash = EXCLUDED.file_hash,
                status = EXCLUDED.status,
                chunk_count = EXCLUDED.chunk_count,
                last_processed_at = NOW(),
                metadata = EXCLUDED.metadata
            """
        )
        with self.state_engine.begin() as conn:
            conn.execute(
                query,
                {
                    "s3_key": s3_key,
                    "file_hash": file_hash,
                    "status": status,
                    "chunk_count": int(chunk_count),
                    "metadata": json.dumps(metadata or {}, ensure_ascii=False),
                },
            )

    def _upsert_chunks_postgres(self, chunks: List, *, s3_key: str, file_hash: str) -> None:
        if self.state_engine is None or not chunks:
            return
        statement = text(
            """
            INSERT INTO ingestion.document_chunks (s3_key, file_hash, chunk_index, content, metadata, source_chunk_id, updated_at)
            VALUES (:s3_key, :file_hash, :chunk_index, :content, CAST(:metadata AS JSONB), :source_chunk_id, NOW())
            ON CONFLICT (s3_key, file_hash, chunk_index)
            DO UPDATE SET
                content = EXCLUDED.content,
                metadata = EXCLUDED.metadata,
                source_chunk_id = EXCLUDED.source_chunk_id,
                updated_at = NOW()
            """
        )
        rows = []
        for chunk in chunks:
            rows.append(
                {
                    "s3_key": s3_key,
                    "file_hash": file_hash,
                    "chunk_index": int(chunk.chunk_index),
                    "content": chunk.text_content,
                    "metadata": json.dumps(chunk.metadata or {}, ensure_ascii=False),
                    "source_chunk_id": chunk.chunk_id,
                }
            )
        with self.state_engine.begin() as conn:
            conn.execute(statement, rows)

    def _load_processed_markers(self) -> set[str]:
        if self.state_engine is None:
            return set(self._state_in_memory)
        query = text("SELECT marker FROM ingestion.worker_processed_objects")
        with self.state_engine.connect() as conn:
            rows = conn.execute(query).fetchall()
        return {r[0] for r in rows}

    def reset_processed_markers(self, key_substring: str) -> int:
        """Delete processed markers for objects containing the provided key substring."""
        if self.state_engine is None:
            old_count = len(self._state_in_memory)
            self._state_in_memory = {
                m for m in self._state_in_memory if key_substring.strip('%') not in m
            }
            deleted = old_count - len(self._state_in_memory)
            logger.info("Reset %s in-memory processed markers for pattern=%s", deleted, key_substring)
            return deleted
        q = text(
            """
            DELETE FROM ingestion.worker_processed_objects
            WHERE object_key ILIKE :pattern
            """
        )
        pattern = f"%{key_substring.strip('%')}%"
        with self.state_engine.begin() as conn:
            result = conn.execute(q, {"pattern": pattern})
        deleted = int(result.rowcount or 0)
        logger.info("Reset %s processed markers for pattern=%s", deleted, pattern)
        return deleted

    def _should_use_s3(self) -> bool:
        """Decide whether the worker should ingest from S3."""
        if self.source_mode == "s3":
            return True
        # auto mode: use S3 whenever bucket is configured
        return bool(getattr(settings, "S3_BUCKET", ""))

    def _build_s3_prefixes(self) -> List[str]:
        """Build candidate S3 prefixes for listing raw ingestion payloads."""
        if self.s3_prefix_filter:
            candidate = f"{self.s3_prefix}/{self.s3_prefix_filter}" if self.s3_prefix else self.s3_prefix_filter
            return [candidate.strip("/")]

        prefixes = [self.s3_raw_prefix, "raws_data"]
        out: List[str] = []
        seen = set()
        for p in prefixes:
            p = p.strip("/")
            if not p:
                continue
            candidate = f"{self.s3_prefix}/{p}" if self.s3_prefix else p
            if candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
        return out

    @staticmethod
    def _build_marker(bucket: str, key: str, etag: Optional[str]) -> str:
        """Marker includes object version (etag) so updated keys get reprocessed."""
        suffix = (etag or "noetag").strip('"')
        return f"s3://{bucket}/{key}#etag={suffix}"

    def run(self, continuous: bool = False):
        """Runs the worker loop.

        Default mode is scraper-stream (generator-based, zero-IO handoff).
        Set `INGESTION_INPUT_MODE=s3` to run legacy S3 scanning mode.
        """
        input_mode = (os.getenv("INGESTION_INPUT_MODE", "scraper_stream") or "scraper_stream").strip().lower()
        if input_mode == "scraper_stream":
            self.run_scraper_stream()
            return

        source_label = "s3"
        logger.info("Starting Ingestion Worker on %s (source=%s)", self.raw_data_dir, source_label)
        
        while True:
            try:
                # 1. Scan source for new objects/files
                new_items: List[dict] = self._scan_s3_objects()
                
                if not new_items:
                    if not continuous:
                        break
                    time.sleep(10) # Poll every 10s
                    continue

                logger.info("Found %s items to process.", len(new_items))
                
                failures = []
                successes = []
                for item in tqdm(new_items, desc="Ingesting"):
                    try:
                        self.process_s3_object(item)
                        successes.append(item.get('marker') or item.get('key'))
                    except Exception as e:
                        logger.error("Failed to process %s: %s", item, e)
                        failures.append({
                            "item": item.get('marker') or item.get('key'),
                            "error": str(e),
                        })

                # After processing batch, save audit report and perform health check
                try:
                    report = {
                        "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                        "processed_count": len(new_items),
                        "success_count": len(successes),
                        "failure_count": len(failures),
                        "failures": failures[:50],
                    }
                    if self.s3_manager:
                        self.s3_manager.save_audit_report(report)

                    # Health check: alert when failure rate exceeds threshold
                    try:
                        from agriconnect.core.settings import settings as app_settings
                        threshold = float(getattr(app_settings, 'INGESTION_AUDIT_FAILURE_THRESHOLD', 0.1) or 0.1)
                    except Exception:
                        threshold = 0.1

                    failure_rate = (len(failures) / max(1, len(new_items)))
                    if failure_rate > threshold:
                        logger.critical("Ingestion failure rate %.2f exceeded threshold %.2f; investigate immediately", failure_rate, threshold)
                except Exception as e:
                    logger.error("Failed to save audit report: %s", e)
                
                if not continuous:
                    break
                    
            except KeyboardInterrupt:
                logger.info("Worker stopped by user.")
                break
            except Exception as e:
                logger.error(f"Critical worker error: {e}")
                if not continuous:
                    break
                time.sleep(30)

    def run_scraper_stream(self) -> dict:
        """Stream scraper results and ingest them one-by-one without buffering."""
        orchestrator = ScraperOrchestrator()
        stats = {"total": 0, "success": 0, "error": 0}

        for source_result in orchestrator.run_all():
            stats["total"] += 1

            if source_result.status == "ERROR":
                logger.error(
                    "Scraper source failed source_id=%s error=%s",
                    source_result.source_id,
                    source_result.error,
                )
                stats["error"] += 1
                continue

            document = source_result.document
            if document is None:
                logger.error("Source returned SUCCESS without document: source_id=%s", source_result.source_id)
                stats["error"] += 1
                continue

            s3_key = self.s3_manager.save_raw(document)
            try:
                self.process_raw_document(document=document, s3_key=s3_key)
                stats["success"] += 1
            except Exception as exc:
                logger.error("Failed ingestion after scrape source_id=%s: %s", source_result.source_id, exc)
                stats["error"] += 1

        logger.info("Scraper-stream ingestion finished: %s", stats)
        return stats

    def _scan_s3_objects(self) -> List[dict]:
        """List new objects in S3 based on processed marker log."""
        if not self.s3_loader:
            return []

        processed_files = self._load_processed_markers()
        return self.s3_loader.list_raw_objects(
            prefixes=self._build_s3_prefixes(),
            processed_markers=processed_files,
            max_files=self.max_files,
        )

    def _scan_files(self) -> List[Path]:
        raise RuntimeError("Local scan disabled. Worker is S3-only.")

    def _mark_processed(self, file_path: Path):
        raise RuntimeError("Local processed marker is disabled. Use S3 marker state.")

    def _mark_processed_marker(self, marker: str, bucket: str, key: str, etag: Optional[str], last_modified: Optional[str]):
        if self.state_engine is None:
            self._state_in_memory.add(marker)
            return
        query = text(
            """
            INSERT INTO ingestion.worker_processed_objects (marker, bucket, object_key, etag, last_modified, processed_at)
            VALUES (:marker, :bucket, :object_key, :etag, :last_modified, NOW())
            ON CONFLICT (marker)
            DO UPDATE SET
                etag = EXCLUDED.etag,
                last_modified = EXCLUDED.last_modified,
                processed_at = NOW()
            """
        )
        with self.state_engine.begin() as conn:
            conn.execute(
                query,
                {
                    "marker": marker,
                    "bucket": bucket,
                    "object_key": key,
                    "etag": etag,
                    "last_modified": last_modified,
                },
            )

    def _detect_type(self, file_ref: Union[Path, str]) -> str:
        """Heuristic to determine processor type."""
        fp_str = str(file_ref).lower()
        suffix = Path(str(file_ref)).suffix
        if "fews" in fp_str or "report" in fp_str:
            return "fews_report"
        if "news" in fp_str:
            return "news_article"
        if "weather" in fp_str or "bulletin" in fp_str:
            return "weather_bulletin"
        if suffix == ".pdf":
            return "pdf"
        if suffix == ".json":
            return "technical_resource"
        return "technical_resource"

    def _extract_text_from_s3_blob(self, key: str, body: bytes) -> str:
        lower = key.lower()
        if lower.endswith((".txt", ".md", ".csv", ".html", ".htm", ".json", ".jsonl")):
            if lower.endswith((".json", ".jsonl")):
                try:
                    obj = json.loads(body.decode("utf-8", errors="replace"))
                    return json.dumps(obj, ensure_ascii=False)
                except Exception:
                    return body.decode("utf-8", errors="replace")
            return body.decode("utf-8", errors="replace")
        if lower.endswith(".pdf"):
            try:
                from pypdf import PdfReader

                reader = PdfReader(io.BytesIO(body))
                return "\n\n".join((page.extract_text() or "") for page in reader.pages)
            except Exception as exc:
                logger.warning("PDF extraction failed for %s: %s", key, exc)
                return ""
        return ""

    def process_file(
        self,
        file_path: Path,
        mark_processed: bool = True,
        source_id_override: Optional[str] = None,
        title_override: Optional[str] = None,
        detect_ref_override: Optional[Union[Path, str]] = None,
    ):
        raise RuntimeError("process_file is disabled in S3-only mode")

    def _process_text_payload(
        self,
        text_payload: str,
        source_id: str,
        source_type: str,
        title: str,
        s3_key: str,
        file_hash: str,
    ) -> int:
        """Processes a text payload end-to-end with deterministic metadata."""
        if not text_payload.strip():
            return 0
        
        raw_doc = RawDocument(
            id=file_hash,
            url=f"s3://{self.s3_manager.bucket}/{s3_key}",
            title=title,
            content_markdown=text_payload,
            metadata={
                "source_id": source_id,
                "source_type": source_type,
                "filename": title,
                "path": source_id,
                "raw_s3_key": s3_key,
            },
            language=None,
        )

        # 2. Get Processor
        processor = get_processor(source_type)

        # Purge existing vectors for this parent_doc_id to avoid orphaned chunks
        try:
            deleted = self.vector_db.delete_by_parent(raw_doc.id)
            if deleted:
                logger.info("Purged %s existing vectors for parent_doc_id=%s before reindex", deleted, raw_doc.id)
        except Exception as e:
            logger.warning("Failed to purge existing vectors for parent_doc_id=%s: %s", raw_doc.id, e)

        # 3. Clean & Chunk (single entrypoint)
        chunks = processor.process(raw_doc)
        
        if not chunks:
            logger.warning("No chunks generated for %s", source_id)
            return 0

        # 4. Embed Chunks
        # Batch embedding
        texts = [c.text_content for c in chunks]
        embeddings = self.embedding_model.get_text_embedding_batch(texts)
        
        for i, chunk in enumerate(chunks):
            chunk.embedding = embeddings[i]
            chunk.metadata = chunk.metadata or {}
            chunk.metadata["s3_key"] = s3_key
            chunk.metadata["file_hash"] = file_hash
            chunk.metadata["chunk_index"] = int(chunk.chunk_index)

        # 5. Double-write: Postgres persistence + vector store cache
        self._upsert_chunks_postgres(chunks, s3_key=s3_key, file_hash=file_hash)
        self.vector_db.upsert_chunks(chunks)
        logger.info("Successfully processed %s (%s chunks)", source_id, len(chunks))
        return len(chunks)

    def process_raw_document(self, document: RawDocument, s3_key: str) -> int:
        """Process a scraper-emitted RawDocument using orchestrator metadata routing."""
        source_type, processor = get_processor_for_document(document)
        chunks = processor.process(document)
        if not chunks:
            logger.warning("No chunks generated for doc_id=%s source_type=%s", document.id, source_type)
            return 0

        texts = [c.text_content for c in chunks]
        embeddings = self.embedding_model.get_text_embedding_batch(texts)
        for i, chunk in enumerate(chunks):
            chunk.embedding = embeddings[i]
            chunk.metadata = chunk.metadata or {}
            chunk.metadata["s3_key"] = s3_key
            chunk.metadata["file_hash"] = document.id
            chunk.metadata["chunk_index"] = int(chunk.chunk_index)

        self._upsert_chunks_postgres(chunks, s3_key=s3_key, file_hash=document.id)
        self.vector_db.upsert_chunks(chunks)
        self._upsert_ingested_document(
            s3_key=s3_key,
            file_hash=document.id,
            chunk_count=len(chunks),
            status="processed",
            metadata={
                "source_id": (document.metadata or {}).get("source_id"),
                "source_type": source_type,
                "title": document.title,
            },
        )
        return len(chunks)

    def process_s3_object(self, obj: dict):
        """Downloads and processes an S3 object fully in-memory."""
        if not self.s3_manager:
            raise RuntimeError("S3 manager not initialized")

        key = obj["key"]
        bucket = obj.get("bucket") or self.s3_manager.bucket
        etag = obj.get("etag")
        marker = obj.get("marker") or f"s3://{self.s3_manager.bucket}/{key}"
        logger.info("Processing S3 object: %s", marker)

        body = self.s3_manager.client.get_object(Bucket=bucket, Key=key)["Body"].read()
        file_hash = self._compute_file_hash(body)
        if self._is_document_unchanged(s3_key=key, file_hash=file_hash):
            logger.info("Skipping unchanged object based on checksum: s3://%s/%s", bucket, key)
            self._mark_processed_marker(
                marker=marker,
                bucket=bucket,
                key=key,
                etag=etag,
                last_modified=str(obj.get("last_modified") or ""),
            )
            return

        key_name = Path(key).name

        # Replay path for schema-versioned raw documents stored as JSON.
        if key.lower().endswith(".json") and self.s3_loader is not None:
            # The loader must return a dict shaped like RawDocument or raise.
            try:
                payload = self.s3_loader.load_json(key)
                raw_doc = RawDocument.model_validate(payload)
                processed_chunks = self.process_raw_document(document=raw_doc, s3_key=key)
                self._upsert_ingested_document(
                    s3_key=key,
                    file_hash=file_hash,
                    chunk_count=processed_chunks,
                    status="processed",
                    metadata={
                        "bucket": bucket,
                        "etag": etag,
                        "marker": marker,
                        "raw_schema_version": (raw_doc.metadata or {}).get("_raw_schema_version"),
                    },
                )
                self._mark_processed_marker(
                    marker=marker,
                    bucket=bucket,
                    key=key,
                    etag=etag,
                    last_modified=str(obj.get("last_modified") or ""),
                )
                logger.info("Successfully replayed RawDocument %s", marker)
                return
            except Exception as exc:
                # Loader must fail loudly; move object to DLQ so ops can inspect and fix upstream
                logger.error("JSON replay decode failed for %s: %s. Moving to DLQ.", marker, exc)
                try:
                    self.s3_manager.move_to_failed(key, reason=str(exc))
                except Exception as e:
                    logger.error("Failed to move %s to DLQ: %s", marker, e)
                return

        source_type = self._detect_type(key)
        text_payload = self._extract_text_from_s3_blob(key, body)

        processed_chunks = self._process_text_payload(
            text_payload=text_payload,
            source_id=marker,
            source_type=source_type,
            title=key_name,
            s3_key=key,
            file_hash=file_hash,
        )
        if processed_chunks <= 0:
            logger.info("No chunk generated for %s. Marking as processed to keep idempotence.", marker)

        self._upsert_ingested_document(
            s3_key=key,
            file_hash=file_hash,
            chunk_count=processed_chunks,
            status="processed",
            metadata={
                "bucket": bucket,
                "etag": etag,
                "marker": marker,
            },
        )

        self._mark_processed_marker(
            marker=marker,
            bucket=bucket,
            key=key,
            etag=etag,
            last_modified=str(obj.get("last_modified") or ""),
        )
        logger.info("Successfully processed %s", marker)


if __name__ == "__main__":
    # Import settings to get default RAW_DATA_DIR
    from agriconnect.rag.config import RAW_DATA_DIR
    from agriconnect.infrastructure.database.db import ensure_ingestion_schema
    
    # Run once by default, use --watch for continuous
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one scan cycle and exit (default)")
    parser.add_argument("--watch", action="store_true", help="Run in continuous watch mode")
    parser.add_argument(
        "--rescan-prefix",
        default="",
        help="Optional object-key substring to reset from processed markers before running (example: weather_advisories)",
    )
    parser.add_argument(
        "--s3-prefix-filter",
        default="",
        help="Optional S3 prefix filter to limit scan scope (example: raw_data/weather_advisories)",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=0,
        help="Optional limit on number of files to process in this run (example: 10)",
    )
    args = parser.parse_args()

    # Bootstrap ingestion DB schema at CLI startup (outside worker core logic).
    resolved_db_url = resolve_database_url(required=False)
    if resolved_db_url:
        try:
            ensure_ingestion_schema(db_url=resolved_db_url)
        except Exception as exc:
            logger.warning("Ingestion schema bootstrap failed at startup: %s", exc)
    else:
        logger.warning("Skipping ingestion schema bootstrap: DATABASE_URL not resolved")

    worker = IngestionWorker(
        raw_data_dir=str(RAW_DATA_DIR),
        s3_prefix_filter=args.s3_prefix_filter,
        max_files=args.max_files,
    )

    if args.rescan_prefix:
        worker.reset_processed_markers(args.rescan_prefix)
    
    worker.run(continuous=args.watch)
