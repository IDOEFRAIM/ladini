from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from airflow import DAG
from sqlalchemy import text

task = importlib.import_module("airflow.decorators").task


logger = logging.getLogger("airflow.agri_ingestion_master")

# Ensure backend/src is importable when Airflow parses this DAG
REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from agriconnect.core.cloud_settings import get_settings
from agriconnect.core.db import get_engine


MAX_DOCS_PER_RUN = 120


def _pipeline_signature(settings) -> str:
    base = "|".join(
        [
            str(getattr(settings, "OPENAI_EMBEDDING_MODEL", "")),
            str(getattr(settings, "OPENAI_EMBEDDING_DIM", "")),
            os.getenv("CHUNK_STRATEGY_VERSION", "sentence_chunk_v1"),
        ]
    )
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def _resolve_chunk_version(settings) -> int:
    explicit = os.getenv("CHUNK_VERSION", "").strip()
    if explicit.isdigit():
        return int(explicit)
    return int(_pipeline_signature(settings)[:8], 16)


def _require_s3(settings) -> None:
    if not getattr(settings, "S3_BUCKET", ""):
        raise RuntimeError("S3_BUCKET must be configured. Airflow ingestion is S3-only.")
    if not getattr(settings, "RAW_DATA_PREFIX", ""):
        raise RuntimeError("RAW_DATA_PREFIX must be configured. Airflow ingestion is S3-only.")


def _s3_client(settings):
    import boto3
    from botocore.config import Config

    session = boto3.session.Session(**settings.boto3_session_kwargs())
    return session.client(
        "s3",
        config=Config(retries={"max_attempts": 8, "mode": "adaptive"}),
    )


def _extract_text_from_object_bytes(blob: bytes, key: str) -> str:
    # Minimal extractor for JSON/TXT/MD/CSV/HTML; PDFs should be handled by dedicated parser task.
    k = key.lower()
    if k.endswith((".txt", ".md", ".csv", ".html", ".htm", ".json", ".jsonl")):
        return blob.decode("utf-8", "replace")
    return ""


def _extract_weather_advisory_fields(blob: bytes, key: str) -> Dict[str, Any]:
    if not key.lower().endswith((".json", ".jsonl")):
        return {}
    try:
        obj = json.loads(blob.decode("utf-8", "replace"))
    except Exception:
        return {}
    if not isinstance(obj, dict):
        return {}
    meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    return {
        "content": str(obj.get("content") or "").strip(),
        "title": str(obj.get("title") or "").strip(),
        "trace_id": str(meta.get("trace_id") or "").strip(),
        "zone": str(meta.get("zone") or "").strip() or None,
        "doc_type": str(meta.get("doc_type") or "weather_advisory"),
    }


def _chunk_text(text: str, chunk_size: int = 1200, overlap: int = 150) -> List[str]:
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]
    out: List[str] = []
    start = 0
    total = len(text)
    while start < total:
        end = min(start + chunk_size, total)
        out.append(text[start:end])
        if end == total:
            break
        start = max(0, end - overlap)
    return out


def _embed_chunks(chunks: List[str], settings) -> List[List[float]]:
    # Use OpenAI embeddings via LangChain when available.
    expected = int(settings.OPENAI_EMBEDDING_DIM)

    def _deterministic_embedding(text: str, dim: int) -> List[float]:
        # Deterministic fallback (no external API): stable vector from SHA-256 stream.
        seed = hashlib.sha256(text.encode("utf-8", errors="ignore")).digest()
        raw = bytearray()
        cursor = seed
        while len(raw) < dim:
            cursor = hashlib.sha256(cursor).digest()
            raw.extend(cursor)
        raw = raw[:dim]
        return [((b / 255.0) * 2.0) - 1.0 for b in raw]

    api_key = str(getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    if not api_key:
        logger.warning("OPENAI_API_KEY missing; using deterministic local embeddings")
        vectors = [_deterministic_embedding(c, expected) for c in chunks]
    else:
        try:
            from langchain_openai import OpenAIEmbeddings

            emb = OpenAIEmbeddings(model=settings.OPENAI_EMBEDDING_MODEL, api_key=api_key)
            vectors = emb.embed_documents(chunks)
        except Exception as exc:
            logger.warning("Embedding backend unavailable, switching to deterministic local fallback: %s", exc)
            vectors = [_deterministic_embedding(c, expected) for c in chunks]

    bad = [i for i, v in enumerate(vectors) if len(v) != expected]
    if bad:
        raise ValueError(f"Embedding dimension mismatch at indices {bad[:10]} (expected={expected})")
    return vectors


with DAG(
    dag_id="agri_ingestion_master",
    description="Master cloud-native ingestion DAG: Scrape -> S3 -> Chunk+Embed -> pgvector",
    start_date=datetime(2026, 3, 21),
    schedule="@hourly",
    catchup=False,
    default_args={
        "owner": "agriconnect",
        "depends_on_past": False,
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
    },
    max_active_runs=1,
    tags=["agri", "ingestion", "pgvector", "s3"],
) as dag:

    @task(task_id="crawl_agricultural_data")
    def crawl_agricultural_data() -> Dict[str, Any]:
        """Run core crawlers and persist a raw snapshot to S3 for downstream ingestion."""
        settings = get_settings()
        _require_s3(settings)

        report: Dict[str, Any] = {
            "weather": {"status": "not_run"},
            "legacy_script": {"status": "not_found"},
        }
        errors: List[str] = []

        # Preferred path: run domain-level weather pipeline (keeps DAG thin)
        try:
            from agriconnect.domain.ingestion import run_weather_pipeline

            weather_result = run_weather_pipeline()
            report["weather"] = {"status": "ok", "result": weather_result}
        except Exception as exc:
            logger.exception("Weather pipeline failed")
            report["weather"] = {"status": "failed", "error": str(exc)}
            errors.append(f"weather_pipeline: {exc}")

        # Backward-compatible optional script execution if it exists.
        script = REPO_ROOT / "backend" / "scripts" / "run_weather_with_env.py"
        if script.exists():
            proc = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT / "backend"),
            )
            report["legacy_script"] = {
                "status": "ok" if proc.returncode == 0 else "failed",
                "returncode": proc.returncode,
                "stdout": (proc.stdout or "")[-4000:],
                "stderr": (proc.stderr or "")[-4000:],
            }
            if proc.returncode != 0:
                errors.append(f"run_weather_with_env.py rc={proc.returncode}")

        # Persist one crawl snapshot so downstream tasks always have a concrete raw artifact.
        scraped_at = datetime.utcnow().isoformat() + "Z"
        snapshot = {
            "scraped_at": scraped_at,
            "report": report,
            "errors": errors,
        }
        snapshot_hour = datetime.utcnow().strftime('%Y%m%dT%H00Z')
        key = f"{settings.RAW_DATA_PREFIX.rstrip('/')}/crawl_snapshots/agri_crawl_{snapshot_hour}.json"
        try:
            s3 = _s3_client(settings)
            s3.put_object(
                Bucket=settings.S3_BUCKET,
                Key=key,
                Body=json.dumps(snapshot, ensure_ascii=False).encode("utf-8"),
                ContentType="application/json",
            )
            snapshot_saved_to = "s3"
        except Exception as exc:
            errors.append(f"s3_snapshot_upload: {exc}")
            raise RuntimeError(f"S3 snapshot upload failed (S3-only mode): {exc}") from exc

        # Fail only if every crawl path failed.
        weather_ok = report.get("weather", {}).get("status") == "ok"
        legacy_ok = report.get("legacy_script", {}).get("status") == "ok"
        if not weather_ok and not legacy_ok:
            raise RuntimeError("All crawl paths failed: " + "; ".join(errors))

        return {
            "status": "ok",
            "bucket": settings.S3_BUCKET,
            "prefix": settings.RAW_DATA_PREFIX,
            "scraped_at": scraped_at,
            "snapshot_key": key,
            "snapshot_saved_to": snapshot_saved_to,
            "errors": errors,
        }

    @task(task_id="process_new_documents")
    def process_new_documents(crawl_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """List candidate documents from S3 raw_data prefix."""
        settings = get_settings()
        _require_s3(settings)
        keys: List[Dict[str, Any]] = []

        try:
            s3 = _s3_client(settings)
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=settings.S3_BUCKET, Prefix=settings.RAW_DATA_PREFIX):
                for obj in page.get("Contents", []):
                    key = obj.get("Key")
                    if not key or key.endswith("/"):
                        continue
                    if "/crawl_snapshots/" in key or key.startswith("crawl_snapshots/"):
                        continue
                    keys.append(
                        {
                            "bucket": settings.S3_BUCKET,
                            "key": key,
                            "size": int(obj.get("Size") or 0),
                            "etag": str(obj.get("ETag") or "").strip('"'),
                            "last_modified": str(obj.get("LastModified") or ""),
                            "source": "s3",
                        }
                    )
        except Exception as exc:
            raise RuntimeError(f"S3 listing failed (S3-only mode): {exc}") from exc

        if not keys:
            logger.warning("No documents found under s3://%s/%s", settings.S3_BUCKET, settings.RAW_DATA_PREFIX)
        elif len(keys) > MAX_DOCS_PER_RUN:
            logger.info("Capping documents for this run: %s -> %s", len(keys), MAX_DOCS_PER_RUN)
            keys = keys[:MAX_DOCS_PER_RUN]
        else:
            keys = sorted(keys, key=lambda x: (x.get("key") or ""))
        return keys

    @task(task_id="smart_chunking_and_embed")
    def smart_chunking_and_embed(objects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Load docs from S3, chunk them, generate embeddings, and prepare upsert payloads."""
        settings = get_settings()
        _require_s3(settings)

        try:
            s3 = _s3_client(settings)
        except Exception as exc:
            raise RuntimeError(f"Unable to initialize S3 client (S3-only mode): {exc}") from exc

        prepared: List[Dict[str, Any]] = []
        chunk_version = _resolve_chunk_version(settings)
        signature = _pipeline_signature(settings)
        embedding_model = str(getattr(settings, "OPENAI_EMBEDDING_MODEL", ""))
        embedding_dim = int(settings.OPENAI_EMBEDDING_DIM)

        for obj in objects:
            key = obj.get("key", "unknown")
            source = "s3"
            bucket = obj["bucket"]
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()

            advisory = _extract_weather_advisory_fields(body, key)
            text = advisory.get("content") or _extract_text_from_object_bytes(body, key)
            if not text.strip():
                continue

            chunks = _chunk_text(text)
            vectors = _embed_chunks(chunks, settings)

            for idx, (chunk, vec) in enumerate(zip(chunks, vectors), start=1):
                md5 = hashlib.md5(chunk.encode("utf-8", errors="ignore")).hexdigest()
                prepared.append(
                    {
                        "doc_ref": key,
                        "doc_type": advisory.get("doc_type") or "raw_data",
                        "category": "ingested",
                        "zone_name": advisory.get("zone"),
                        "content": chunk,
                        "content_md5": md5,
                        "embedding": vec,
                        "chunk_version": chunk_version,
                        "metadata": {
                            "source": source,
                            "s3_bucket": obj.get("bucket"),
                            "s3_key": key,
                            "s3_etag": obj.get("etag"),
                            "chunk_index": idx,
                            "pipeline_signature": signature,
                            "embedding_model": embedding_model,
                            "embedding_dim": embedding_dim,
                            "source_trace_id": advisory.get("trace_id") or None,
                            "ingested_at": datetime.utcnow().isoformat() + "Z",
                        },
                        "source_uri": f"s3://{obj.get('bucket')}/{key}",
                    }
                )

        return prepared

    @task(task_id="upsert_to_pgvector")
    def upsert_to_pgvector(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Atomic upsert into agri_vector.document_chunks with md5-based conflict key."""
        settings = get_settings()
        db_url = settings.resolve_database_url()
        if not db_url:
            raise RuntimeError("DATABASE_URL not resolved from env/Airflow/Secrets")
        engine = get_engine(db_url)

        sql = """
            INSERT INTO agri_vector.document_chunks (
                doc_ref, doc_type, category, zone_name, content, content_md5,
                embedding, metadata, source_uri, chunk_version, is_active
            ) VALUES (
                :doc_ref, :doc_type, :category, :zone_name, :content, :content_md5,
                CAST(:embedding_literal AS vector), CAST(:metadata_json AS jsonb), :source_uri, :chunk_version, TRUE
            )
            ON CONFLICT (doc_ref, content_md5, chunk_version)
            DO UPDATE SET
                content = EXCLUDED.content,
                embedding = EXCLUDED.embedding,
                metadata = EXCLUDED.metadata,
                source_uri = EXCLUDED.source_uri,
                is_active = TRUE
        """

        inserted = 0
        skipped = 0

        with engine.begin() as conn:
            # Ensure destination schema/table exists to avoid failing on fresh environments.
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS agri_vector"))
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS agri_vector.document_chunks (
                        id BIGSERIAL PRIMARY KEY,
                        doc_ref TEXT NOT NULL,
                        doc_type TEXT,
                        category TEXT,
                        zone_name TEXT,
                        content TEXT NOT NULL,
                        content_md5 TEXT NOT NULL,
                        embedding vector,
                        metadata JSONB,
                        source_uri TEXT,
                        chunk_version INTEGER NOT NULL DEFAULT 1,
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_document_chunks_ref_md5_ver
                    ON agri_vector.document_chunks (doc_ref, content_md5, chunk_version)
                    """
                )
            )

            for r in rows:
                emb = r.get("embedding") or []
                if len(emb) != int(settings.OPENAI_EMBEDDING_DIM):
                    skipped += 1
                    continue

                payload = {
                    **r,
                    "embedding_literal": "[" + ",".join(str(float(x)) for x in emb) + "]",
                    "metadata_json": json.dumps(r.get("metadata") or {}, ensure_ascii=False),
                }
                conn.execute(text(sql), payload)
                inserted += 1

        return {"status": "ok", "rows_upserted": inserted, "rows_skipped": skipped}

    c = crawl_agricultural_data()
    n = process_new_documents(c)
    e = smart_chunking_and_embed(n)
    upsert_to_pgvector(e)
