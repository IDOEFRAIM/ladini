from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from airflow import DAG

task = importlib.import_module("airflow.decorators").task


logger = logging.getLogger("airflow.agri_ingestion_master")

# Ensure backend/src is importable when Airflow parses this DAG
REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from agriconnect.core.cloud_settings import get_settings


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
    try:
        from langchain_openai import OpenAIEmbeddings

        emb = OpenAIEmbeddings(model=settings.OPENAI_EMBEDDING_MODEL, api_key=settings.OPENAI_API_KEY)
        vectors = emb.embed_documents(chunks)
    except Exception as exc:
        raise RuntimeError(f"Embedding backend unavailable: {exc}") from exc

    expected = int(settings.OPENAI_EMBEDDING_DIM)
    bad = [i for i, v in enumerate(vectors) if len(v) != expected]
    if bad:
        raise ValueError(f"Embedding dimension mismatch at indices {bad[:10]} (expected={expected})")
    return vectors


with DAG(
    dag_id="agri_ingestion_master",
    description="Master cloud-native ingestion DAG: Scrape -> S3 -> Chunk+Embed -> pgvector",
    start_date=datetime(2026, 3, 21),
    schedule_interval="@hourly",
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
        """Trigger scrapers/orchestrators that push raw artifacts to S3."""
        settings = get_settings()
        script = REPO_ROOT / "backend" / "scripts" / "run_weather_with_env.py"
        rc = os.system(f'"{sys.executable}" "{script}"')
        if rc != 0:
            raise RuntimeError(f"Scraper orchestration failed with rc={rc}")

        return {
            "status": "ok",
            "bucket": settings.S3_BUCKET,
            "prefix": settings.RAW_DATA_PREFIX,
            "scraped_at": datetime.utcnow().isoformat() + "Z",
        }

    @task(task_id="process_new_documents")
    def process_new_documents(_: Dict[str, Any]) -> List[Dict[str, Any]]:
        """List candidate documents from S3 raw_data prefix."""
        settings = get_settings()
        if not settings.S3_BUCKET:
            raise RuntimeError("S3_BUCKET is not configured")

        s3 = _s3_client(settings)
        paginator = s3.get_paginator("list_objects_v2")
        keys: List[Dict[str, Any]] = []

        for page in paginator.paginate(Bucket=settings.S3_BUCKET, Prefix=settings.RAW_DATA_PREFIX):
            for obj in page.get("Contents", []):
                key = obj.get("Key")
                if not key or key.endswith("/"):
                    continue
                keys.append(
                    {
                        "bucket": settings.S3_BUCKET,
                        "key": key,
                        "size": int(obj.get("Size") or 0),
                        "etag": str(obj.get("ETag") or "").strip('"'),
                        "last_modified": str(obj.get("LastModified") or ""),
                    }
                )

        if not keys:
            logger.warning("No documents found under s3://%s/%s", settings.S3_BUCKET, settings.RAW_DATA_PREFIX)
        return keys

    @task(task_id="smart_chunking_and_embed")
    def smart_chunking_and_embed(objects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Load S3 docs, chunk them, generate embeddings, and prepare upsert payloads."""
        settings = get_settings()
        s3 = _s3_client(settings)

        prepared: List[Dict[str, Any]] = []

        for obj in objects:
            bucket = obj["bucket"]
            key = obj["key"]
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            text = _extract_text_from_object_bytes(body, key)
            if not text.strip():
                continue

            chunks = _chunk_text(text)
            vectors = _embed_chunks(chunks, settings)

            for idx, (chunk, vec) in enumerate(zip(chunks, vectors), start=1):
                md5 = hashlib.md5(chunk.encode("utf-8", errors="ignore")).hexdigest()
                prepared.append(
                    {
                        "doc_ref": key,
                        "doc_type": "raw_data",
                        "category": "ingested",
                        "zone_name": None,
                        "content": chunk,
                        "content_md5": md5,
                        "embedding": vec,
                        "metadata": {
                            "s3_bucket": bucket,
                            "s3_key": key,
                            "chunk_index": idx,
                            "ingested_at": datetime.utcnow().isoformat() + "Z",
                        },
                        "source_uri": f"s3://{bucket}/{key}",
                    }
                )

        return prepared

    @task(task_id="upsert_to_pgvector")
    def upsert_to_pgvector(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Atomic upsert into agri_vector.document_chunks with md5-based conflict key."""
        import psycopg2
        from psycopg2.extras import Json

        settings = get_settings()
        db_url = settings.resolve_database_url()
        if not db_url:
            raise RuntimeError("DATABASE_URL not resolved from env/Airflow/Secrets")

        sql = """
            INSERT INTO agri_vector.document_chunks (
                doc_ref, doc_type, category, zone_name, content, content_md5,
                embedding, metadata, source_uri, chunk_version, is_active
            ) VALUES (
                %(doc_ref)s, %(doc_type)s, %(category)s, %(zone_name)s, %(content)s, %(content_md5)s,
                %(embedding_literal)s::vector, %(metadata)s, %(source_uri)s, 1, TRUE
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

        conn = psycopg2.connect(db_url)
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                for r in rows:
                    emb = r.get("embedding") or []
                    if len(emb) != int(settings.OPENAI_EMBEDDING_DIM):
                        skipped += 1
                        continue

                    payload = {
                        **r,
                        "embedding_literal": "[" + ",".join(str(float(x)) for x in emb) + "]",
                        "metadata": Json(r.get("metadata") or {}),
                    }
                    cur.execute(sql, payload)
                    inserted += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        return {"status": "ok", "rows_upserted": inserted, "rows_skipped": skipped}

    c = crawl_agricultural_data()
    n = process_new_documents(c)
    e = smart_chunking_and_embed(n)
    upsert_to_pgvector(e)
