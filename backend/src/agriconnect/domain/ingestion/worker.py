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
import functools
import uuid
import socket
import re
from urllib.parse import urlparse
from pathlib import Path
from typing import List, Optional, Union
from urllib.parse import unquote_plus

from tqdm import tqdm
from sqlalchemy import text, bindparam
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import Vector

from agriconnect.rag.components import get_embedding_model
from agriconnect.core.db import get_engine, resolve_database_url
from agriconnect.core.schemas import DocumentChunk, RawDocument

from agriconnect.domain.ingestion.processors.factory import get_processor, get_processor_for_document
from agriconnect.domain.ingestion.text_cleaner import TextCleaner
from agriconnect.domain.ingestion.storage.vector_db import VectorDBManager
from agriconnect.domain.ingestion.storage.s3_manager import S3Manager
from agriconnect.domain.ingestion.loaders.s3_loader import S3Loader
from agriconnect.services.scraper.scrapers.jobs.pdf_downloader import PdfDownloader
from agriconnect.core.settings import settings

logger = logging.getLogger("ingestion_worker")
logging.basicConfig(level=logging.INFO)


class CriticalDimensionMismatch(Exception):
    """Raised when an embedding vector has an unexpected dimension."""

    def __init__(self, expected: int, actual: int, chunk_id: str | None = None):
        detail = f" expected={expected}, actual={actual}"
        if chunk_id:
            detail = f" chunk_id={chunk_id},{detail}"
        super().__init__(f"Critical embedding dimension mismatch:{detail}")
        self.expected = int(expected)
        self.actual = int(actual)
        self.chunk_id = str(chunk_id or "")


def retry(max_attempts: int = 3, delay_seconds: float = 1.0):
    def deco(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    last = exc
                    if attempt >= max_attempts:
                        break
                    time.sleep(delay_seconds)
            raise last

        return wrapper

    return deco

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
        self.embedding_batch_size = max(1, int(os.getenv("INGESTION_EMBED_BATCH_SIZE", "32") or "32"))
        self.embedding_retry_attempts = max(1, int(os.getenv("INGESTION_EMBED_RETRY_MAX", "5") or "5"))
        self.embedding_retry_base_delay = float(os.getenv("INGESTION_EMBED_RETRY_BASE_DELAY", "1.0") or "1.0")
        self.expected_embedding_dim = int(getattr(settings, "RAG_EMBEDDING_DIM", 768) or 768)
        self.embedding_version = (os.getenv("AGRICONNECT_EMBEDDING_VERSION", "v1") or "v1").strip()
        configured_chunks_table = (os.getenv("AGRICONNECT_CHUNKS_TABLE", "") or "").strip()
        if configured_chunks_table:
            self.chunks_table = configured_chunks_table
        elif self.embedding_version.lower() == "v2":
            self.chunks_table = "ingestion.document_chunks_v2"
        else:
            self.chunks_table = "ingestion.document_chunks"
        self.s3_manager: Optional[S3Manager] = None
        self.s3_loader: Optional[S3Loader] = None
        self.max_files = max(0, int(max_files or 0))
        self.db_url = resolve_database_url(required=False)
        self.state_engine = None
        self._state_in_memory: set[str] = set()
        # Fail fast: require a reachable state DB at worker startup to preserve idempotence.
        if not self.db_url:
            raise RuntimeError(
                "DATABASE_URL not configured: ingestion worker requires a state DB for idempotent acquisition"
            )
        try:
            self.state_engine = get_engine(self.db_url)
        except Exception as exc:
            # Hard failure: do not silently fall back to in-memory markers.
            raise RuntimeError(
                f"State DB unavailable at startup; aborting ingestion worker: {exc}"
            ) from exc
        self._validate_chunks_table_name()
        # Determine available unique constraints and preferred conflict target
        self._ensure_chunks_table_and_unique_lock()
        self._detect_chunks_unique_constraints()
        self._verify_embedding_alignment()
        self.s3_prefix = (getattr(settings, "S3_KEY_PREFIX", "") or "").strip("/")
        self.s3_raw_prefix = os.getenv("S3_RAW_PREFIX", "raws_data").strip("/")
        self.allow_legacy_raw_data = (
            os.getenv("INGESTION_ALLOW_LEGACY_RAW_DATA", "0") or "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.consumer_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.pdf_downloader = PdfDownloader(config={})
        if self.s3_raw_prefix == "raw_data":
            logger.warning(
                "S3_RAW_PREFIX=raw_data is deprecated. Prefer raws_data and JSON envelopes only."
            )
        self.s3_prefix_filter = (s3_prefix_filter or "").strip("/")
        self.harvest_scout_run_id = (os.getenv("HARVEST_SCOUT_RUN_ID", "") or "").strip()
        self.harvest_source_id = (os.getenv("HARVEST_SOURCE_ID", "") or "").strip()
        self.pdf_storage_path = Path(os.getenv("PDF_STORAGE_PATH", "/app/data/pdfs"))
        self.pdf_storage_path.mkdir(parents=True, exist_ok=True)
        self.local_raw_dir = self.pdf_storage_path / "raws_data"
        self.local_raw_dir.mkdir(parents=True, exist_ok=True)

        self.source_mode = (os.getenv("INGESTION_SOURCE", "local") or "local").lower()
        if self.source_mode not in {"auto", "s3", "local"}:
            logger.warning("Unknown INGESTION_SOURCE=%s, falling back to auto", self.source_mode)
            self.source_mode = "auto"

        if self._should_use_s3():
            try:
                self.s3_manager = S3Manager()
                self.s3_loader = S3Loader(self.s3_manager)
                # Early connectivity check
                self.s3_manager.client.head_bucket(Bucket=self.s3_manager.bucket)
                logger.info("S3 ingestion enabled on bucket=%s", self.s3_manager.bucket)
            except Exception as e:
                raise RuntimeError(f"S3 ingestion required but unavailable: {e}") from e

        if self._should_use_s3() and not self.s3_manager:
            raise RuntimeError("S3 manager not initialized while INGESTION_SOURCE requires S3.")

    def _validate_chunks_table_name(self) -> None:
        # Accept strictly schema.table identifiers only to avoid SQL injection via env.
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*", self.chunks_table):
            raise RuntimeError(f"Invalid AGRICONNECT_CHUNKS_TABLE value: {self.chunks_table}")

    def _ensure_chunks_table_and_unique_lock(self) -> None:
        if self.state_engine is None:
            return
        q_exists = text("SELECT to_regclass(:tbl)")
        q_unique = text(
            """
            SELECT 1
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN unnest(c.conkey) AS ck(attnum) ON true
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ck.attnum
            WHERE c.contype = 'u'
              AND n.nspname = :schema_name
              AND t.relname = :table_name
              AND a.attname = 'content_hash'
            LIMIT 1
            """
        )
        schema_name, table_name = self.chunks_table.split(".", 1)
        with self.state_engine.connect() as conn:
            exists = conn.execute(q_exists, {"tbl": self.chunks_table}).scalar()
            if not exists:
                raise RuntimeError(
                    f"Chunks table {self.chunks_table} not found. Run migrations before starting ingestion."
                )
            has_unique = conn.execute(
                q_unique,
                {"schema_name": schema_name, "table_name": table_name},
            ).first()
            # Keep backward-compatible check: at least one of the deterministic keys
            # (content_hash or chunk_id) should be present as a UNIQUE constraint.
            if not has_unique:
                logger.warning(
                    "No UNIQUE(content_hash) constraint detected on %s. Will attempt dynamic conflict-target resolution at startup.",
                    self.chunks_table,
                )

    def _ensure_chunks_table_schema_contract(self) -> None:
        """Validate required Postgres column types before ingestion traffic."""
        if self.state_engine is None:
            return

        schema_name, table_name = self.chunks_table.split(".", 1)
        expected_vector_type = f"vector({int(self.expected_embedding_dim)})"
        q_col_type = text(
            """
            SELECT udt_name
            FROM information_schema.columns
            WHERE table_schema = :schema_name
              AND table_name = :table_name
              AND column_name = :column_name
            LIMIT 1
            """
        )
        q_embedding_type = text(
            """
            SELECT format_type(a.atttypid, a.atttypmod)
            FROM pg_attribute a
            JOIN pg_class c ON a.attrelid = c.oid
            JOIN pg_namespace n ON c.relnamespace = n.oid
            WHERE n.nspname = :schema_name
              AND c.relname = :table_name
              AND a.attname = :column_name
              AND a.attnum > 0
              AND NOT a.attisdropped
            LIMIT 1
            """
        )
        with self.state_engine.connect() as conn:
            emb_type = conn.execute(
                q_embedding_type,
                {
                    "schema_name": schema_name,
                    "table_name": table_name,
                    "column_name": "embedding",
                },
            ).scalar()
            meta_type = conn.execute(
                q_col_type,
                {
                    "schema_name": schema_name,
                    "table_name": table_name,
                    "column_name": "metadata",
                },
            ).scalar()

        if str(emb_type or "").lower() != expected_vector_type:
            raise RuntimeError(
                f"Safety boot failed [DB Check]: invalid embedding column type on {self.chunks_table}: "
                f"expected {expected_vector_type}, got {emb_type!r}"
            )
        if str(meta_type or "").lower() != "jsonb":
            raise RuntimeError(
                f"Safety boot failed [DB Check]: invalid metadata column type on {self.chunks_table}: expected jsonb, got {meta_type!r}"
            )

    def _verify_embedding_alignment(self) -> None:
        """Safety boot: verify DB vector schema and embedding model output dim are aligned."""
        self._ensure_chunks_table_schema_contract()

        expected = int(self.expected_embedding_dim)
        try:
            probe = self.embedding_model.get_text_embedding("test")
        except Exception as exc:
            raise RuntimeError(
                "Safety boot failed [Model Check]: unable to generate test embedding for 'test'. "
                f"Expected model output dim={expected}. Underlying error: {exc}"
            ) from exc

        actual = len(probe or [])
        if actual != expected:
            raise RuntimeError(
                "Safety boot failed [Model Check]: embedding dimension mismatch. "
                f"Expected {expected}, got {actual}. "
                "Update EMBEDDING_MODEL/RAG_EMBEDDING_DIM or DB schema before startup."
            )

        logger.info(
            "Safety boot passed: embedding alignment verified (db/model dim=%s)",
            expected,
        )

    def _detect_chunks_unique_constraints(self) -> None:
        """Detect UNIQUE constraints on the chunks table and pick a preferred conflict target.

        Sets `self._preferred_conflict_target` to either 'chunk_id' or 'content_hash'.
        If neither exists, logs a warning and leaves the default as 'content_hash'.
        """
        if self.state_engine is None:
            self._preferred_conflict_target = "content_hash"
            return

        schema_name, table_name = self.chunks_table.split(".", 1)
        q = text(
            """
            SELECT a.attname
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN unnest(c.conkey) AS ck(attnum) ON true
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ck.attnum
            WHERE c.contype = 'u'
              AND n.nspname = :schema_name
              AND t.relname = :table_name
            """
        )
        with self.state_engine.connect() as conn:
            rows = conn.execute(q, {"schema_name": schema_name, "table_name": table_name}).fetchall()

        unique_cols = {r[0] for r in rows} if rows else set()
        # Prefer chunk_id (stronger deterministic primary) when available
        if "chunk_id" in unique_cols:
            self._preferred_conflict_target = "chunk_id"
        elif "content_hash" in unique_cols:
            self._preferred_conflict_target = "content_hash"
        else:
            # Fallback to content_hash but warn: may be unsafe until migration applied
            logger.warning(
                "No UNIQUE constraint detected on 'chunk_id' or 'content_hash' for %s. Falling back to 'content_hash'.",
                self.chunks_table,
            )
            self._preferred_conflict_target = "content_hash"
        logger.info("Preferred chunk upsert conflict target: %s", self._preferred_conflict_target)

    def _validate_embedding_dimension(self, embedding: List[float], chunk_id: str | None = None) -> None:
        actual = len(embedding or [])
        expected = int(self.expected_embedding_dim)
        if actual != expected:
            raise CriticalDimensionMismatch(expected=expected, actual=actual, chunk_id=chunk_id)

    def _validate_chunks_embeddings_dimensions(self, chunks: List[DocumentChunk]) -> None:
        for chunk in chunks:
            self._validate_embedding_dimension(list(chunk.embedding or []), chunk_id=str(chunk.chunk_id or ""))

    def _register_acquisition_job(self, *, s3_key: str, file_hash: str) -> bool:
        if self.state_engine is None:
            return True
        q = text(
            """
            INSERT INTO ingestion.acquisition_jobs (s3_key, file_hash, status, extract_started_at, updated_at)
            VALUES (:s3_key, :file_hash, 'PROCESSING', NOW(), NOW())
            ON CONFLICT (file_hash) DO NOTHING
            """
        )
        with self.state_engine.begin() as conn:
            result = conn.execute(q, {"s3_key": s3_key, "file_hash": file_hash})
        return bool(result.rowcount and result.rowcount > 0)

    def _update_acquisition_job(self, *, file_hash: str, status: str, stage: Optional[str] = None, error_type: Optional[str] = None, error_message: Optional[str] = None) -> None:
        if self.state_engine is None:
            return
        fields = ["status = :status", "updated_at = NOW()"]
        params = {"file_hash": file_hash, "status": status}
        if error_type is not None:
            fields.append("error_type = :error_type")
            params["error_type"] = error_type
        if error_message is not None:
            fields.append("error_message = :error_message")
            params["error_message"] = error_message[:4000]
        if stage == "extract_start":
            fields.append("extract_started_at = NOW()")
        elif stage == "extract_end":
            fields.append("extract_finished_at = NOW()")
        elif stage == "embed_start":
            fields.append("embedding_started_at = NOW()")
        elif stage == "embed_end":
            fields.append("embedding_finished_at = NOW()")
        elif stage == "upsert_start":
            fields.append("upsert_started_at = NOW()")
        elif stage == "upsert_end":
            fields.append("upsert_finished_at = NOW()")

        q = text(f"UPDATE ingestion.acquisition_jobs SET {', '.join(fields)} WHERE file_hash = :file_hash")
        with self.state_engine.begin() as conn:
            conn.execute(q, params)

    @staticmethod
    def _is_retryable_embedding_error(exc: Exception) -> bool:
        txt = str(exc or "").lower()
        retryable_markers = [
            "429",
            "rate limit",
            "quota",
            "timeout",
            "timed out",
            "connection",
            "network",
            "temporarily unavailable",
            "service unavailable",
            "502",
            "503",
            "504",
            "500",
        ]
        return any(marker in txt for marker in retryable_markers)

    def _embed_batch_with_retry(self, texts: List[str]):
        last_exc: Optional[Exception] = None
        max_attempts = int(self.embedding_retry_attempts)
        for attempt in range(1, max_attempts + 1):
            try:
                return self.embedding_model.get_text_embedding_batch(texts)
            except Exception as exc:
                last_exc = exc
                if attempt >= max_attempts or not self._is_retryable_embedding_error(exc):
                    raise
                delay = float(self.embedding_retry_base_delay) * (2 ** (attempt - 1))
                logger.warning(
                    "Embedding batch failed (attempt %s/%s). Retrying in %.2fs. reason=%s",
                    attempt,
                    max_attempts,
                    delay,
                    exc,
                )
                time.sleep(delay)
        if last_exc is not None:
            raise last_exc
        return []

    def _embed_texts_in_batches(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        out: List[List[float]] = []
        batch_size = int(self.embedding_batch_size)
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            batch_embeddings = self._embed_batch_with_retry(batch)
            out.extend(batch_embeddings)
        return out

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
        error_type: Optional[str] = None,
        conn=None,
    ) -> None:
        if self.state_engine is None:
            return
        query = text(
            """
            INSERT INTO ingestion.ingested_documents (s3_key, file_hash, status, chunk_count, last_processed_at, metadata, error_type)
            VALUES (:s3_key, :file_hash, :status, :chunk_count, NOW(), :metadata, :error_type)
            ON CONFLICT (s3_key)
            DO UPDATE SET
                file_hash = EXCLUDED.file_hash,
                status = EXCLUDED.status,
                chunk_count = EXCLUDED.chunk_count,
                last_processed_at = NOW(),
                metadata = EXCLUDED.metadata,
                error_type = EXCLUDED.error_type
            """
        ).bindparams(bindparam("metadata", type_=JSONB))
        params = {
            "s3_key": s3_key,
            "file_hash": file_hash,
            "status": status,
            "chunk_count": int(chunk_count),
            "metadata": dict(metadata or {}),
            "error_type": error_type,
        }
        if conn is not None:
            conn.execute(query, params)
            return
        with self.state_engine.begin() as c:
            c.execute(query, params)

    def _update_acquisition_job_final(
        self,
        *,
        file_hash: str,
        status: str,
        chunk_count: int,
        processing_duration_ms: int,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        if self.state_engine is None:
            return
        q = text(
            """
            UPDATE ingestion.acquisition_jobs
            SET status = :status,
                chunk_count = :chunk_count,
                processing_duration_ms = :processing_duration_ms,
                error_type = :error_type,
                error_message = :error_message,
                updated_at = NOW()
            WHERE file_hash = :file_hash
            """
        )
        params = {
            "file_hash": file_hash,
            "status": status,
            "chunk_count": int(chunk_count),
            "processing_duration_ms": int(processing_duration_ms),
            "error_type": error_type,
            "error_message": (error_message or "")[:4000] or None,
        }
        try:
            with self.state_engine.begin() as conn:
                conn.execute(q, params)
        except Exception:
            # Backward-compatible fallback when optional columns do not exist yet.
            self._update_acquisition_job(
                file_hash=file_hash,
                status=status,
                error_type=error_type,
                error_message=error_message,
            )

    @staticmethod
    def _classify_error_type(exc: Exception) -> str:
        if isinstance(exc, CriticalDimensionMismatch):
            return "VECTOR_DIM_MISMATCH"
        txt = str(exc or "").lower()
        exc_name = type(exc).__name__.lower()
        if "timeout" in txt or "connection" in txt or "network" in txt:
            return "CONNECTION_ERROR"
        if "dim" in txt and ("mismatch" in txt or "vector" in txt):
            return "VECTOR_DIM_MISMATCH"
        if "selector" in txt:
            return "SELECTOR_FAILED"
        if "validation" in exc_name or "valueerror" in exc_name:
            return "VALIDATION_ERROR"
        return "PROCESSING_ERROR"

    @staticmethod
    def _bucket_for_raw_s3_url(url: str) -> str:
        parsed = urlparse(url or "")
        if parsed.scheme == "s3" and parsed.netloc:
            return parsed.netloc
        return ""

    @staticmethod
    def _build_context_prefix(title: str, metadata: dict) -> str:
        """Build a compact semantic anchor for embedding input."""
        meta = metadata or {}
        country = meta.get("country") or meta.get("region") or meta.get("zone") or "unknown"
        date_value = (
            meta.get("publication_date")
            or meta.get("date")
            or meta.get("observed_at")
            or "unknown"
        )
        clean_title = (title or "Untitled").strip()
        return f"[DOC: {clean_title} | DATE: {date_value} | REGION: {country}] "

    @staticmethod
    def _normalize_source_url(url: str) -> str:
        raw = (url or "").strip()
        if not raw:
            return ""
        return raw.rstrip("/")

    @staticmethod
    def _normalize_chunk_text(text_value: str) -> str:
        return re.sub(r"\s+", " ", (text_value or "").strip())

    def _recompute_chunk_identity(
        self,
        *,
        parent_doc_id: str,
        source_url: str,
        chunk_index: int,
        text_value: str,
    ) -> tuple[str, str]:
        normalized_url = self._normalize_source_url(source_url)
        normalized_text = self._normalize_chunk_text(text_value)
        content_payload = f"{normalized_url}\n{normalized_text}"
        content_hash = hashlib.sha256(content_payload.encode("utf-8")).hexdigest()
        chunk_payload = f"{parent_doc_id}:{content_hash}:{int(chunk_index)}"
        chunk_id = hashlib.sha256(chunk_payload.encode("utf-8")).hexdigest()
        return content_hash, chunk_id

    def _upsert_chunks_postgres(
        self,
        chunks: List,
        *,
        s3_key: str,
        file_hash: str,
        trace_id: Optional[str] = None,
        conn=None,
        with_embeddings: bool = True,
    ) -> None:
        if self.state_engine is None or not chunks:
            return

        # Build upsert statements dynamically using detected conflict target.
        def _build_statements(conflict_target: str):
            # sanitize column name (we validated table name earlier)
            ct = conflict_target
            s_with_trace = text(
                f"""
                INSERT INTO {self.chunks_table} (
                    s3_key, file_hash, trace_id, chunk_index, content, metadata, source_chunk_id, content_hash, embedding, is_indexed, updated_at
                )
                VALUES (
                    :s3_key,
                    :file_hash,
                    :trace_id,
                    :chunk_index,
                    :content,
                    :metadata,
                    :source_chunk_id,
                    :content_hash,
                    :embedding,
                    :is_indexed,
                    NOW()
                )
                ON CONFLICT ({ct})
                DO UPDATE SET
                    content = EXCLUDED.content,
                    metadata = EXCLUDED.metadata,
                    source_chunk_id = EXCLUDED.source_chunk_id,
                    trace_id = EXCLUDED.trace_id,
                    embedding = EXCLUDED.embedding,
                    is_indexed = (COALESCE({self.chunks_table}.is_indexed, false) OR COALESCE(EXCLUDED.is_indexed, false)),
                    updated_at = NOW()
                """
            ).bindparams(
                bindparam("metadata", type_=JSONB),
                bindparam("embedding", type_=Vector(self.expected_embedding_dim)),
            )
            s_with = text(
                f"""
                INSERT INTO {self.chunks_table} (
                    s3_key, file_hash, chunk_index, content, metadata, source_chunk_id, content_hash, embedding, is_indexed, updated_at
                )
                VALUES (
                    :s3_key,
                    :file_hash,
                    :chunk_index,
                    :content,
                    :metadata,
                    :source_chunk_id,
                    :content_hash,
                    :embedding,
                    :is_indexed,
                    NOW()
                )
                ON CONFLICT ({ct})
                DO UPDATE SET
                    content = EXCLUDED.content,
                    metadata = EXCLUDED.metadata,
                    source_chunk_id = EXCLUDED.source_chunk_id,
                    embedding = EXCLUDED.embedding,
                    is_indexed = (COALESCE({self.chunks_table}.is_indexed, false) OR COALESCE(EXCLUDED.is_indexed, false)),
                    updated_at = NOW()
                """
            ).bindparams(
                bindparam("metadata", type_=JSONB),
                bindparam("embedding", type_=Vector(self.expected_embedding_dim)),
            )
            s_wo_trace = text(
                f"""
                INSERT INTO {self.chunks_table} (
                    s3_key, file_hash, trace_id, chunk_index, content, metadata, source_chunk_id, content_hash, is_indexed, updated_at
                )
                VALUES (:s3_key, :file_hash, :trace_id, :chunk_index, :content, :metadata, :source_chunk_id, :content_hash, :is_indexed, NOW())
                ON CONFLICT ({ct})
                DO UPDATE SET
                    content = EXCLUDED.content,
                    metadata = EXCLUDED.metadata,
                    source_chunk_id = EXCLUDED.source_chunk_id,
                    trace_id = EXCLUDED.trace_id,
                    is_indexed = (COALESCE({self.chunks_table}.is_indexed, false) OR COALESCE(EXCLUDED.is_indexed, false)),
                    updated_at = NOW()
                """
            ).bindparams(bindparam("metadata", type_=JSONB))
            s_wo = text(
                f"""
                INSERT INTO {self.chunks_table} (
                    s3_key, file_hash, chunk_index, content, metadata, source_chunk_id, content_hash, is_indexed, updated_at
                )
                VALUES (:s3_key, :file_hash, :chunk_index, :content, :metadata, :source_chunk_id, :content_hash, :is_indexed, NOW())
                ON CONFLICT ({ct})
                DO UPDATE SET
                    content = EXCLUDED.content,
                    metadata = EXCLUDED.metadata,
                    source_chunk_id = EXCLUDED.source_chunk_id,
                    is_indexed = (COALESCE({self.chunks_table}.is_indexed, false) OR COALESCE(EXCLUDED.is_indexed, false)),
                    updated_at = NOW()
                """
            ).bindparams(bindparam("metadata", type_=JSONB))
            return s_with_trace, s_with, s_wo_trace, s_wo

        rows = []
        for chunk in chunks:
            emb = [float(x) for x in list(chunk.embedding or [])]
            if with_embeddings:
                self._validate_embedding_dimension(emb, chunk_id=str(chunk.chunk_id or ""))
            rows.append(
                {
                    "s3_key": s3_key,
                    "file_hash": file_hash,
                    "chunk_index": int(chunk.chunk_index),
                    "content": chunk.text_content,
                    "metadata": dict(chunk.metadata or {}),
                    "source_chunk_id": chunk.chunk_id,
                    "content_hash": chunk.content_hash,
                    "embedding": emb if with_embeddings else None,
                    "trace_id": trace_id,
                    "is_indexed": False,
                }
            )

        # Always sort payload rows deterministically to reduce deadlock risk across parallel writers
        rows.sort(key=lambda r: (r.get("content_hash") or "", int(r.get("chunk_index") or 0)))

        def _execute_candidate(connection, statement, rows_payload, label: str) -> None:
            tx = connection.begin_nested() if connection.in_transaction() else connection.begin()
            try:
                connection.execute(statement, rows_payload)
                tx.commit()
            except Exception:
                try:
                    tx.rollback()
                finally:
                    logger.exception("Chunk upsert candidate failed: %s", label)
                raise

        def _execute_with_connection(connection):
            last_exc = None
            if with_embeddings:
                # Build candidates based on preferred conflict target and fallback
                preferred = getattr(self, "_preferred_conflict_target", "content_hash")
                fallback = "content_hash" if preferred == "chunk_id" else "chunk_id"
                try_targets = [preferred, fallback]
                candidates = []
                for tgt in try_targets:
                    stmts = _build_statements(tgt)
                    candidates.extend([
                        (f"{tgt}:with_embedding_trace", stmts[0]),
                        (f"{tgt}:with_embedding", stmts[1]),
                        (f"{tgt}:without_embedding_trace", stmts[2]),
                        (f"{tgt}:without_embedding", stmts[3]),
                    ])
            else:
                preferred = getattr(self, "_preferred_conflict_target", "content_hash")
                fallback = "content_hash" if preferred == "chunk_id" else "chunk_id"
                try_targets = [preferred, fallback]
                candidates = []
                for tgt in try_targets:
                    stmts = _build_statements(tgt)
                    candidates.extend([
                        (f"{tgt}:without_embedding_trace", stmts[2]),
                        (f"{tgt}:without_embedding", stmts[3]),
                    ])

            for label, stmt in candidates:
                try:
                    _execute_candidate(connection, stmt, rows, label)
                    return
                except Exception as exc:
                    last_exc = exc
                    # If conflict on chosen column arises, try next candidate (fallback)
                    logger.warning("Upsert candidate %s failed; trying next fallback. reason=%s", label, exc)
                    continue

            if last_exc is not None:
                raise last_exc

        if conn is not None:
            _execute_with_connection(conn)
            return

        with self.state_engine.connect() as connection:
            _execute_with_connection(connection)

    def _build_clean_chunks(
        self,
        document: RawDocument,
        *,
        s3_key: str,
        trace_id: Optional[str] = None,
    ) -> tuple[str, list[DocumentChunk]]:
        source_type, processor = get_processor_for_document(document)
        chunks = processor.process(document)
        if not chunks:
            return source_type, []

        for chunk in chunks:
            cleaned = TextCleaner.clean(chunk.text_content)
            chunk.text_content = cleaned
            source_url = str((chunk.metadata or {}).get("source_url") or document.url)
            content_hash, chunk_id = self._recompute_chunk_identity(
                parent_doc_id=document.id,
                source_url=source_url,
                chunk_index=int(chunk.chunk_index),
                text_value=cleaned,
            )
            chunk.content_hash = content_hash
            chunk.chunk_id = chunk_id
            chunk.metadata = chunk.metadata or {}
            chunk.metadata["s3_key"] = s3_key
            chunk.metadata["file_hash"] = document.id
            chunk.metadata["chunk_index"] = int(chunk.chunk_index)
            if trace_id:
                chunk.metadata["trace_id"] = trace_id

        return source_type, chunks

    def _delete_existing_chunks_for_parent_doc(self, *, parent_doc_id: str, conn) -> int:
        if not parent_doc_id:
            return 0
        # To avoid deadlocks when multiple workers delete/insert concurrently,
        # select deterministic ordering of identifiers and delete them in that order.
        select_q = text(
            f"""
            SELECT content_hash
            FROM {self.chunks_table}
            WHERE metadata->>'document_id' = :parent_doc_id
            ORDER BY content_hash
            """
        )
        rows = conn.execute(select_q, {"parent_doc_id": str(parent_doc_id)}).fetchall()
        deleted = 0
        for r in rows:
            ch = r[0]
            if not ch:
                continue
            dq = text(f"DELETE FROM {self.chunks_table} WHERE content_hash = :content_hash")
            res = conn.execute(dq, {"content_hash": ch})
            deleted += int(res.rowcount or 0)
        return deleted

    def process_raw_document_silver_only(
        self,
        document: RawDocument,
        s3_key: str,
        *,
        trace_id: Optional[str] = None,
        conn=None,
    ) -> tuple[int, str]:
        source_type, chunks = self._build_clean_chunks(document, s3_key=s3_key, trace_id=trace_id)
        if not chunks:
            logger.warning("No chunks generated for doc_id=%s source_type=%s", document.id, source_type)
            return 0, source_type

        self._upsert_chunks_postgres(
            chunks,
            s3_key=s3_key,
            file_hash=document.id,
            trace_id=trace_id,
            conn=conn,
            with_embeddings=False,
        )
        return len(chunks), source_type

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
        if self.source_mode == "local":
            return False
        # auto mode: use S3 whenever bucket is configured
        return bool(getattr(settings, "S3_BUCKET", ""))

    def _save_pdf_locally(self, *, payload: bytes, source_id: str, pdf_url: str) -> str:
        digest = hashlib.sha256(payload).hexdigest()[:16]
        filename = Path(urlparse(pdf_url).path).name or "document.pdf"
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"
        target_dir = self.pdf_storage_path / source_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{digest}_{filename}"
        target_path.write_bytes(payload)
        return str(target_path)

    def _save_raw_locally(self, *, raw_doc: RawDocument, source_id: str) -> str:
        target_dir = self.local_raw_dir / source_id
        target_dir.mkdir(parents=True, exist_ok=True)
        raw_path = target_dir / f"{raw_doc.id}.json"
        try:
            serialized = raw_doc.model_dump(mode="json")
        except Exception:
            serialized = raw_doc.dict()
        raw_path.write_text(json.dumps(serialized, ensure_ascii=False), encoding="utf-8")
        return str(raw_path)

    def _build_s3_prefixes(self) -> List[str]:
        """Build candidate S3 prefixes for listing raw ingestion payloads."""
        if self.s3_prefix_filter:
            candidate = f"{self.s3_prefix}/{self.s3_prefix_filter}" if self.s3_prefix else self.s3_prefix_filter
            return [candidate.strip("/")]

        prefixes = [self.s3_raw_prefix]
        if self.allow_legacy_raw_data and "raw_data" not in prefixes:
            prefixes.append("raw_data")
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
        Modes:
        - `scraper_stream` (default): Scout then Harvest in a single process.
        - `scout`: Scout-only, enqueue discovery URLs (no downloads).
        - `harvest`: Harvest-only, consume discovery_queue and ingest.
        - `s3`: legacy S3 scanning mode.
        """
        input_mode = (os.getenv("INGESTION_INPUT_MODE", "scraper_stream") or "scraper_stream").strip().lower()
        if input_mode in {"scraper_stream", "scraper-stream"}:
            self.run_scraper_stream()
            return
        if input_mode == "scout":
            self.run_scout()
            return
        if input_mode == "harvest":
            self.run_harvest()
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
        """Scout + Harvest pipeline.

        Scout: Scrapers only enqueue PDF URLs into ingestion.discovery_queue.
        Harvest: Worker consumes pending URLs, validates binary PDF, then ingests.
        """
        scout_stats = self.run_scout()
        harvest_stats = self.run_harvest()
        stats = {
            **dict(scout_stats or {}),
            **{f"harvest_{k}": v for k, v in dict(harvest_stats or {}).items()},
        }
        logger.info("Scraper-stream ingestion finished: %s", stats)
        return stats

    @staticmethod
    def _classify_harvest_rejection(exc: Exception) -> str:
        msg = (str(exc) or exc.__class__.__name__ or "").strip()
        lower = msg.lower()

        # Strict canonical reason required by ops spec.
        if "does not start with %pdf" in lower or "invalid or wrapped content" in lower or "invalid_binary_header" in lower:
            return "INVALID_BINARY_HEADER"

        if "http 403" in lower:
            return "HTTP_403"
        if "http 429" in lower:
            return "HTTP_429"
        if isinstance(exc, PermissionError) or "circuit open" in lower:
            return "CIRCUIT_OPEN"
        if "timeout" in lower:
            return "TIMEOUT"

        return "HARVEST_ERROR"

    def run_scout(self, *, sources: Optional[List[str]] = None) -> dict:
        """Scout-only: run scrapers and enqueue discovery URLs (no downloads)."""
        from agriconnect.services.scraper.scraper_orchestrator import ScraperOrchestrator

        orchestrator = ScraperOrchestrator()
        stats = {
            "sources_total": 0,
            "sources_success": 0,
            "sources_error": 0,
            "urls_enqueued": 0,
        }

        source_ids = sources if sources else None
        if source_ids:
            for source_id in source_ids:
                stats["sources_total"] += 1
                res = orchestrator.run_source(source_id)
                if res.status == "ERROR":
                    stats["sources_error"] += 1
                    logger.error("Scout source failed source_id=%s error=%s", res.source_id, res.error)
                    continue
                stats["sources_success"] += 1
                stats["urls_enqueued"] += int(res.discovered_count or 0)
            logger.info("Scout finished (filtered sources=%s): %s", source_ids, stats)
            return stats

        for source_result in orchestrator.run_all():
            stats["sources_total"] += 1
            if source_result.status == "ERROR":
                stats["sources_error"] += 1
                logger.error(
                    "Scout source failed source_id=%s error=%s",
                    source_result.source_id,
                    source_result.error,
                )
                continue

            stats["sources_success"] += 1
            stats["urls_enqueued"] += int(source_result.discovered_count or 0)

        logger.info("Scout finished: %s", stats)
        return stats

    def run_harvest(self, *, max_items: int = 0) -> dict:
        """Harvest-only: consume discovery_queue and ingest PDFs.

        Logs every rejection and writes a canonical error reason into discovery_queue.error_message.
        """
        stats = {
            "claimed": 0,
            "processed": 0,
            "rejected": 0,
            "rejected_invalid_binary_header": 0,
            "rejected_http_403": 0,
            "rejected_http_429": 0,
            "rejected_timeout": 0,
            "rejected_circuit_open": 0,
            "failed_other": 0,
        }

        processed_count = 0
        while True:
            if max_items and processed_count >= int(max_items):
                break
            claimed = self._claim_next_discovery_target()
            if not claimed:
                break

            stats["claimed"] += 1
            queue_id = int(claimed["id"])
            pdf_url = str(claimed["url_pdf"])
            source_id = str(claimed.get("source_id") or "unknown")
            queue_meta = claimed.get("metadata") if isinstance(claimed.get("metadata"), dict) else {}

            try:
                payload, dl_meta = self.pdf_downloader._download_pdf_bytes(pdf_url)
                if not payload.startswith(b"%PDF"):
                    raise RuntimeError("invalid_binary_header")

                raw_doc = self._build_raw_document_from_pdf_payload(
                    payload=payload,
                    pdf_url=pdf_url,
                    source_id=source_id,
                    source_metadata=queue_meta,
                    download_metadata=dl_meta,
                )

                pdf_local_path = self._save_pdf_locally(
                    payload=payload,
                    pdf_url=pdf_url,
                    source_id=source_id,
                )
                raw_doc.metadata = raw_doc.metadata or {}
                raw_doc.metadata["pdf_local_path"] = pdf_local_path
                raw_local_path = self._save_raw_locally(raw_doc=raw_doc, source_id=source_id)
                raw_marker = f"local://{Path(raw_local_path).as_posix()}"

                # Persist harvested artifact pointers for auditability.
                self._update_discovery_target_metadata(
                    queue_id,
                    {
                        "pdf_local_path": pdf_local_path,
                        "raw_local_path": raw_local_path,
                    },
                )
                self.process_raw_document(document=raw_doc, s3_key=raw_marker)
                self._mark_discovery_target_processed(queue_id)
                stats["processed"] += 1
                processed_count += 1
            except Exception as exc:
                reason = self._classify_harvest_rejection(exc)
                stats["rejected"] += 1

                if reason == "INVALID_BINARY_HEADER":
                    stats["rejected_invalid_binary_header"] += 1
                elif reason == "HTTP_403":
                    stats["rejected_http_403"] += 1
                elif reason == "HTTP_429":
                    stats["rejected_http_429"] += 1
                elif reason == "TIMEOUT":
                    stats["rejected_timeout"] += 1
                elif reason == "CIRCUIT_OPEN":
                    stats["rejected_circuit_open"] += 1
                else:
                    stats["failed_other"] += 1

                logger.warning(
                    "Harvest rejected queue_id=%s source_id=%s url=%s reason=%s error=%s",
                    queue_id,
                    source_id,
                    pdf_url,
                    reason,
                    (str(exc) or exc.__class__.__name__),
                )
                self._mark_discovery_target_failed(queue_id, reason)

        logger.info("Harvest finished: %s", stats)
        return stats

    def _build_raw_document_from_pdf_payload(
        self,
        *,
        payload: bytes,
        pdf_url: str,
        source_id: str,
        source_metadata: dict,
        download_metadata: dict,
    ) -> RawDocument:
        markdown, ex_stats = self.pdf_downloader.extractor.extract_markdown(payload)
        if not (markdown or "").strip():
            raise RuntimeError("empty_extraction")

        parsed = urlparse(pdf_url or "")
        title = Path(parsed.path or "document.pdf").name or "document.pdf"
        file_hash = self._compute_file_hash(payload)
        metadata = {
            "source_id": source_id,
            "source_type": "pdf",
            "source_url": pdf_url,
            "pdf_url": pdf_url,
            "total_pages": int((ex_stats or {}).get("total_pages", 0)),
            "extraction_engine": (ex_stats or {}).get("extraction_engine", "unknown"),
            "bytes_downloaded": int((download_metadata or {}).get("bytes_downloaded", len(payload))),
            "binary_validated": True,
            "discovery_metadata": dict(source_metadata or {}),
        }
        return RawDocument(
            id=file_hash,
            url=pdf_url,
            title=title,
            content_markdown=markdown,
            metadata=metadata,
            language=None,
        )

    def _claim_next_discovery_target(self) -> Optional[dict]:
        if self.state_engine is None:
            return None
        conditions = ["status = 'pending'"]
        params = {"locked_by": self.consumer_id}
        if self.harvest_source_id:
            conditions.append("source_id = :harvest_source_id")
            params["harvest_source_id"] = self.harvest_source_id
        if self.harvest_scout_run_id:
            conditions.append("metadata->>'scout_run_id' = :harvest_scout_run_id")
            params["harvest_scout_run_id"] = self.harvest_scout_run_id

        where_clause = " AND ".join(conditions)
        q = text(
            f"""
            WITH candidate AS (
                SELECT id
                FROM ingestion.discovery_queue
                WHERE {where_clause}
                ORDER BY discovered_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE ingestion.discovery_queue q
            SET status = 'processing',
                attempts = q.attempts + 1,
                locked_at = NOW(),
                locked_by = :locked_by,
                updated_at = NOW(),
                error_message = NULL
            FROM candidate
            WHERE q.id = candidate.id
            RETURNING q.id, q.url_pdf, q.source_id, q.metadata
            """
        )
        with self.state_engine.begin() as conn:
            row = conn.execute(q, params).mappings().first()
        return dict(row) if row else None

    def _mark_discovery_target_processed(self, queue_id: int) -> None:
        if self.state_engine is None:
            return
        q = text(
            """
            UPDATE ingestion.discovery_queue
            SET status = 'processed',
                processed_at = NOW(),
                updated_at = NOW()
            WHERE id = :id
            """
        )
        with self.state_engine.begin() as conn:
            conn.execute(q, {"id": int(queue_id)})

    def _update_discovery_target_metadata(self, queue_id: int, patch: dict) -> None:
        """Merge metadata patch into discovery_queue.metadata (JSONB)."""
        if self.state_engine is None:
            return
        if not isinstance(patch, dict) or not patch:
            return
        q = text(
            """
            UPDATE ingestion.discovery_queue
            SET metadata = COALESCE(metadata, '{}'::jsonb) || :patch,
                updated_at = NOW()
            WHERE id = :id
            """
        ).bindparams(bindparam("patch", type_=JSONB))
        with self.state_engine.begin() as conn:
            conn.execute(q, {"id": int(queue_id), "patch": dict(patch)})

    def _mark_discovery_target_failed(self, queue_id: int, reason: str) -> None:
        if self.state_engine is None:
            return
        q = text(
            """
            UPDATE ingestion.discovery_queue
            SET status = 'failed',
                error_message = :error_message,
                updated_at = NOW()
            WHERE id = :id
            """
        )
        with self.state_engine.begin() as conn:
            conn.execute(q, {"id": int(queue_id), "error_message": (reason or "unknown")[:4000]})

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

        # 4. Embed Chunks (batched)
        # Batch embedding
        for chunk in chunks:
            cleaned = TextCleaner.clean(chunk.text_content)
            chunk.text_content = cleaned
            source_url = str((chunk.metadata or {}).get("source_url") or raw_doc.url)
            content_hash, chunk_id = self._recompute_chunk_identity(
                parent_doc_id=raw_doc.id,
                source_url=source_url,
                chunk_index=int(chunk.chunk_index),
                text_value=cleaned,
            )
            chunk.content_hash = content_hash
            chunk.chunk_id = chunk_id

        texts = [f"{self._build_context_prefix(raw_doc.title, c.metadata)}{c.text_content}" for c in chunks]
        embed_start = time.perf_counter()
        embeddings = self._embed_texts_in_batches(texts)
        embed_elapsed_ms = int((time.perf_counter() - embed_start) * 1000)
        logger.debug("Embedding phase completed for s3_key=%s: %dms (chunks=%d)", s3_key, embed_elapsed_ms, len(chunks))
        
        for i, chunk in enumerate(chunks):
            chunk.embedding = embeddings[i]
            self._validate_embedding_dimension(list(chunk.embedding or []), chunk_id=str(chunk.chunk_id or ""))
            chunk.metadata = chunk.metadata or {}
            chunk.metadata["s3_key"] = s3_key
            chunk.metadata["file_hash"] = file_hash
            chunk.metadata["chunk_index"] = int(chunk.chunk_index)

        # 5. Double-write: Postgres persistence + vector store cache
        if self.state_engine is not None:
            sql_start = time.perf_counter()
            with self.state_engine.begin() as conn:
                self._delete_existing_chunks_for_parent_doc(parent_doc_id=raw_doc.id, conn=conn)
                self._upsert_chunks_postgres(
                    chunks,
                    s3_key=s3_key,
                    file_hash=file_hash,
                    conn=conn,
                    with_embeddings=True,
                )
            sql_elapsed_ms = int((time.perf_counter() - sql_start) * 1000)
            logger.debug("SQL phase (delete+upsert) completed for s3_key=%s: %dms", s3_key, sql_elapsed_ms)
        else:
            self._upsert_chunks_postgres(chunks, s3_key=s3_key, file_hash=file_hash, with_embeddings=True)
        try:
            self.vector_db.upsert_chunks(chunks)
            # Mark chunks as indexed only after successful vector-store insertion
            if self.state_engine is not None:
                hashes = [c.content_hash for c in chunks if c.content_hash]
                if hashes:
                    q = text(f"UPDATE {self.chunks_table} SET is_indexed = TRUE WHERE content_hash = ANY(:hashes)")
                    with self.state_engine.begin() as conn:
                        conn.execute(q, {"hashes": hashes})
        except Exception as e:
            logger.error("Vector-store upsert failed for %s: %s", source_id, e)
            raise
        logger.info("Successfully processed %s (%s chunks)", source_id, len(chunks))
        return len(chunks)

    def process_raw_document(self, document: RawDocument, s3_key: str, trace_id: Optional[str] = None) -> int:
        """Process a scraper-emitted RawDocument using orchestrator metadata routing."""
        started_at = time.perf_counter()
        source_type, chunks = self._build_clean_chunks(document, s3_key=s3_key, trace_id=trace_id)
        if not chunks:
            logger.warning("No chunks generated for doc_id=%s source_type=%s", document.id, source_type)
            return 0

        texts = [f"{self._build_context_prefix(document.title, c.metadata)}{c.text_content}" for c in chunks]
        embed_start = time.perf_counter()
        embeddings = self._embed_texts_in_batches(texts)
        embed_elapsed_ms = int((time.perf_counter() - embed_start) * 1000)
        logger.debug("Embedding phase completed for document_id=%s: %dms (chunks=%d)", document.id, embed_elapsed_ms, len(chunks))
        for i, chunk in enumerate(chunks):
            chunk.embedding = embeddings[i]
            self._validate_embedding_dimension(list(chunk.embedding or []), chunk_id=str(chunk.chunk_id or ""))

        if self.state_engine is not None:
            sql_start = time.perf_counter()
            with self.state_engine.begin() as conn:
                self._delete_existing_chunks_for_parent_doc(parent_doc_id=document.id, conn=conn)
                self._upsert_chunks_postgres(
                    chunks,
                    s3_key=s3_key,
                    file_hash=document.id,
                    trace_id=trace_id,
                    with_embeddings=True,
                    conn=conn,
                )
                self._upsert_ingested_document(
                    s3_key=s3_key,
                    file_hash=document.id,
                    chunk_count=len(chunks),
                    status="processed",
                    metadata={
                        "source_id": (document.metadata or {}).get("source_id"),
                        "source_type": source_type,
                        "title": document.title,
                        "trace_id": trace_id or (document.metadata or {}).get("trace_id"),
                        "processing_duration_ms": int((time.perf_counter() - started_at) * 1000),
                    },
                    conn=conn,
                )
            sql_elapsed_ms = int((time.perf_counter() - sql_start) * 1000)
            logger.debug("SQL phase (delete+upsert+ingested_doc) completed for document_id=%s: %dms", document.id, sql_elapsed_ms)
        else:
            sql_start = time.perf_counter()
            self._upsert_chunks_postgres(
                chunks,
                s3_key=s3_key,
                file_hash=document.id,
                trace_id=trace_id,
                with_embeddings=True,
            )
            sql_elapsed_ms = int((time.perf_counter() - sql_start) * 1000)
            logger.debug("SQL phase (upsert) completed for document_id=%s: %dms", document.id, sql_elapsed_ms)
        try:
            # Vector cleanup first to avoid orphan vectors when a document shrinks.
            self.vector_db.delete_by_parent(document.id)
            self.vector_db.upsert_chunks(chunks)
            # mark indexed true only after vector-store insertion succeeded
            if self.state_engine is not None:
                hashes = [c.content_hash for c in chunks if c.content_hash]
                if hashes:
                    q = text(f"UPDATE {self.chunks_table} SET is_indexed = TRUE WHERE content_hash = ANY(:hashes)")
                    with self.state_engine.begin() as conn:
                        conn.execute(q, {"hashes": hashes})
        except Exception as e:
            logger.error("Vector-store upsert failed for file_hash=%s: %s", document.id, e)
            raise
        return len(chunks)

    def process_s3_object(self, obj: dict):
        """Downloads and processes an S3 object fully in-memory."""
        if not self.s3_manager:
            raise RuntimeError("S3 manager not initialized")

        key = obj["key"]
        bucket = obj.get("bucket") or self.s3_manager.bucket
        etag = obj.get("etag")
        marker = obj.get("marker") or f"s3://{self.s3_manager.bucket}/{key}"
        started_at = time.perf_counter()
        logger.info("Processing S3 object: %s", marker)

        body = self.s3_manager.client.get_object(Bucket=bucket, Key=key)["Body"].read()
        file_hash = self._compute_file_hash(body)
        if not self._register_acquisition_job(s3_key=key, file_hash=file_hash):
            logger.info("Acquisition lock exists for file_hash=%s. Skipping object %s", file_hash, marker)
            return
        self._update_acquisition_job(file_hash=file_hash, status="PROCESSING", stage="extract_start")
        if self._is_document_unchanged(s3_key=key, file_hash=file_hash):
            logger.info("Skipping unchanged object based on checksum: s3://%s/%s", bucket, key)
            self._update_acquisition_job(file_hash=file_hash, status="SKIPPED", stage="extract_end")
            self._update_acquisition_job_final(
                file_hash=file_hash,
                status="SKIPPED",
                chunk_count=0,
                processing_duration_ms=int((time.perf_counter() - started_at) * 1000),
            )
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
                self._update_acquisition_job(file_hash=file_hash, status="PROCESSING", stage="extract_end")
                self._update_acquisition_job(file_hash=file_hash, status="PROCESSING", stage="embed_start")
                processed_chunks = self.process_raw_document(document=raw_doc, s3_key=key)
                self._update_acquisition_job(file_hash=file_hash, status="PROCESSING", stage="embed_end")
                self._update_acquisition_job(file_hash=file_hash, status="PROCESSING", stage="upsert_start")
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
                self._update_acquisition_job(file_hash=file_hash, status="PROCESSED", stage="upsert_end")
                self._update_acquisition_job_final(
                    file_hash=file_hash,
                    status="PROCESSED",
                    chunk_count=processed_chunks,
                    processing_duration_ms=int((time.perf_counter() - started_at) * 1000),
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
                err_type = self._classify_error_type(exc)
                logger.error("JSON replay decode failed for %s: %s. Moving to DLQ.", marker, exc)
                self._update_acquisition_job(
                    file_hash=file_hash,
                    status="FAILED",
                    error_type=err_type,
                    error_message=str(exc),
                )
                self._update_acquisition_job_final(
                    file_hash=file_hash,
                    status="FAILED",
                    chunk_count=0,
                    processing_duration_ms=int((time.perf_counter() - started_at) * 1000),
                    error_type=err_type,
                    error_message=str(exc),
                )
                self._upsert_ingested_document(
                    s3_key=key,
                    file_hash=file_hash,
                    chunk_count=0,
                    status="failed",
                    metadata={
                        "bucket": bucket,
                        "etag": etag,
                        "marker": marker,
                        "error": str(exc),
                    },
                    error_type=err_type,
                )
                try:
                    self.s3_manager.move_to_failed(key, reason=f"{err_type}:{exc}")
                except Exception as e:
                    logger.error("Failed to move %s to DLQ: %s", marker, e)
                return

        source_type = self._detect_type(key)
        text_payload = self._extract_text_from_s3_blob(key, body)

        # Legacy non-JSON objects are wrapped into RawDocument JSON under raws_data
        # so downstream processing only consumes structured payloads.
        raw_doc = self.s3_manager.wrap_as_raw_document(
            content_markdown=text_payload,
            source_id=marker,
            source_type=source_type,
            title=key_name,
            file_hash=file_hash,
            raw_s3_key=key,
            bucket=bucket,
        )
        wrapped_s3_key = self.s3_manager.save_raw(raw_doc, prefix=self.s3_raw_prefix)
        try:
            processed_chunks = self.process_raw_document(document=raw_doc, s3_key=wrapped_s3_key)
            self._update_acquisition_job(file_hash=file_hash, status="PROCESSED", stage="upsert_end")
            self._update_acquisition_job_final(
                file_hash=file_hash,
                status="PROCESSED",
                chunk_count=processed_chunks,
                processing_duration_ms=int((time.perf_counter() - started_at) * 1000),
            )
        except Exception as exc:
            err_type = self._classify_error_type(exc)
            logger.error("Legacy object processing failed for %s: %s", marker, exc)
            self._update_acquisition_job(
                file_hash=file_hash,
                status="FAILED",
                error_type=err_type,
                error_message=str(exc),
            )
            self._update_acquisition_job_final(
                file_hash=file_hash,
                status="FAILED",
                chunk_count=0,
                processing_duration_ms=int((time.perf_counter() - started_at) * 1000),
                error_type=err_type,
                error_message=str(exc),
            )
            self._upsert_ingested_document(
                s3_key=wrapped_s3_key,
                file_hash=file_hash,
                chunk_count=0,
                status="failed",
                metadata={
                    "bucket": bucket,
                    "etag": etag,
                    "marker": marker,
                    "legacy_source_key": key,
                    "error": str(exc),
                    "raw_s3_key": (raw_doc.metadata or {}).get("raw_s3_key"),
                },
                error_type=err_type,
            )
            try:
                dlq_key = (raw_doc.metadata or {}).get("raw_s3_key") or key
                if dlq_key:
                    self.s3_manager.move_to_failed(dlq_key, reason=f"{err_type}:{exc}")
            except Exception as dlq_exc:
                logger.error("Failed to move raw payload to DLQ for %s: %s", marker, dlq_exc)
            raise
        if processed_chunks <= 0:
            logger.info("No chunk generated for %s. Marking as processed to keep idempotence.", marker)

        self._upsert_ingested_document(
            s3_key=wrapped_s3_key,
            file_hash=file_hash,
            chunk_count=processed_chunks,
            status="processed",
            metadata={
                "bucket": bucket,
                "etag": etag,
                "marker": marker,
                "legacy_source_key": key,
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

    def process_s3_event_message(
        self,
        *,
        bucket_name: str,
        file_key: str,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> None:
        """Bridge helper for S3->SQS event consumers running on Fargate."""
        normalized_key = unquote_plus(str(file_key or "").strip())
        if not normalized_key:
            raise ValueError("file_key is required")
        self.process_s3_object(
            {
                "bucket": str(bucket_name or self.s3_manager.bucket),
                "key": normalized_key,
                "etag": etag,
                "last_modified": last_modified,
                "marker": f"s3://{str(bucket_name or self.s3_manager.bucket)}/{normalized_key}",
            }
        )


def process_s3_event_record(
    *,
    bucket_name: str,
    file_key: str,
    etag: Optional[str] = None,
    last_modified: Optional[str] = None,
    raw_data_dir: Optional[str] = None,
) -> None:
    """Function entrypoint for service-layer S3 event consumers."""
    from agriconnect.rag.config import RAW_DATA_DIR

    worker = IngestionWorker(raw_data_dir=raw_data_dir or str(RAW_DATA_DIR))
    worker.process_s3_event_message(
        bucket_name=bucket_name,
        file_key=file_key,
        etag=etag,
        last_modified=last_modified,
    )


if __name__ == "__main__":
    # Import settings to get default RAW_DATA_DIR
    from agriconnect.rag.config import RAW_DATA_DIR
    from agriconnect.infrastructure.database.db import ensure_ingestion_schema
    
    # Run once by default, use --watch for continuous
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        default="",
        choices=["", "scraper_stream", "scout", "harvest", "s3"],
        help="Execution mode override (default: env INGESTION_INPUT_MODE or scraper_stream)",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help="Optional source id filter for --mode scout (repeatable).",
    )
    parser.add_argument(
        "--max-queue-items",
        type=int,
        default=0,
        help="Optional max items to process for --mode harvest (0 = drain).",
    )
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

    # Mode resolution: CLI override > env > default.
    resolved_mode = (args.mode or os.getenv("INGESTION_INPUT_MODE", "scraper_stream") or "scraper_stream").strip().lower()
    if resolved_mode == "scout":
        # Scout-only path intentionally avoids initializing IngestionWorker
        # to preserve strict separation from Harvest dependencies.
        from agriconnect.services.scraper.scraper_orchestrator import ScraperOrchestrator
        orchestrator = ScraperOrchestrator()
        selected_sources = list(args.source or [])
        if selected_sources:
            for sid in selected_sources:
                res = orchestrator.run_source(sid)
                if res.status == "ERROR":
                    logger.error("Scout source failed source_id=%s error=%s", res.source_id, res.error)
                else:
                    logger.info("Scout source ok source_id=%s discovered_count=%s", res.source_id, int(res.discovered_count or 0))
        else:
            for res in orchestrator.run_all():
                if res.status == "ERROR":
                    logger.error("Scout source failed source_id=%s error=%s", res.source_id, res.error)
                else:
                    logger.info("Scout source ok source_id=%s discovered_count=%s", res.source_id, int(res.discovered_count or 0))
    elif resolved_mode == "harvest":
        worker = IngestionWorker(
            raw_data_dir=str(RAW_DATA_DIR),
            s3_prefix_filter=args.s3_prefix_filter,
            max_files=args.max_files,
        )
        if args.rescan_prefix:
            worker.reset_processed_markers(args.rescan_prefix)
        worker.run_harvest(max_items=int(args.max_queue_items or 0))
    else:
        # Preserve previous behavior (scraper_stream default, s3 legacy).
        worker = IngestionWorker(
            raw_data_dir=str(RAW_DATA_DIR),
            s3_prefix_filter=args.s3_prefix_filter,
            max_files=args.max_files,
        )
        if args.rescan_prefix:
            worker.reset_processed_markers(args.rescan_prefix)
        os.environ["INGESTION_INPUT_MODE"] = resolved_mode
        worker.run(continuous=args.watch)
