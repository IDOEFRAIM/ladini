"""
Manual execution script for the Ingestion Pipeline.
Bypasses Airflow to run the ETL logic directly on local machine (Windows compatible).
"""
import sys
import os
import logging
from pathlib import Path
import json
import hashlib
import uuid
from typing import List, Dict, Any

# --- Bootstrapping ---
def _bootstrap_path():
    # backend/scripts/run_ingestion_manual.py -> backend/
    repo_root = Path(__file__).resolve().parents[1] 
    src = repo_root / "src"
    
    # 1. Add 'backend/src' for 'agriconnect' package
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
        
    # 2. Add 'AgriConnect' (project root) to allow 'from backend.ingestion...'
    project_root = repo_root.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

_bootstrap_path()
# ---------------------

# Configure Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ManualIngest")

from backend.ingestion.schema import RawDocument, DocumentChunk
from backend.ingestion.storage.s3_manager import S3Manager
from backend.ingestion.storage.vector_db import VectorDBManager
from agriconnect.core.settings import settings

def run_pipeline():
    logger.info("=== Starting Manual Ingestion Pipeline ===")

    # 1. EXTRACT
    raw_docs = []
    
    # Weather
    try:
        from backend.ingestion.collectors.weather_collector import WeatherCollector
        logger.info("Running WeatherCollector...")
        wc = WeatherCollector()
        docs = wc.run()
        raw_docs.extend(docs)
        logger.info(f"Weather: {len(docs)} docs")
    except Exception as e:
        logger.error(f"Weather failed: {e}")

    # News
    try:
        from backend.ingestion.collectors.news_collector import NewsCollector
        logger.info("Running NewsCollector...")
        nc = NewsCollector()
        docs = nc.run()
        raw_docs.extend(docs)
        logger.info(f"News: {len(docs)} docs")
    except Exception as e:
        logger.error(f"News failed: {e}")

    # FEWS
    try:
        from backend.ingestion.collectors.fews_collector import FewsCollector
        logger.info("Running FewsCollector...")
        fc = FewsCollector()
        docs = fc.run()
        raw_docs.extend(docs)
        logger.info(f"FEWS: {len(docs)} docs")
    except Exception as e:
        logger.error(f"FEWS failed: {e}")

    logger.info(f"Total Raw Documents: {len(raw_docs)}")

    # 2. LOAD (S3)
    logger.info("=== Uploading to S3 ===")
    s3 = S3Manager()
    s3_processed_docs = []
    for d_dict in raw_docs:
        try:
            doc = RawDocument(**d_dict)
            key = s3.upload_raw_json(doc)
            doc.raw_s3_key = key
            s3_processed_docs.append(doc)
        except Exception as e:
            logger.error(f"S3 Upload failed for {d_dict.get('source_id')}: {e}")

    # 3. TRANSFORM (Chunking)
    logger.info("=== Chunking & Hashing ===")
    chunks = []
    chunk_size = 1000
    for doc in s3_processed_docs:
        text = doc.content
        if not text: 
            continue
            
        # Simple splitting
        raw_text_chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
        
        for idx, chunk_text in enumerate(raw_text_chunks):
            if not chunk_text.strip():
                continue
            
            chunk_hash = hashlib.md5(chunk_text.encode("utf-8")).hexdigest()
            chunk_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, chunk_hash))
            
            c = DocumentChunk(
                chunk_id=chunk_id,
                parent_doc_source_id=doc.source_id,
                chunk_index=idx,
                text_content=chunk_text,
                content_hash=chunk_hash,
                metadata=doc.metadata
            )
            chunks.append(c)

    logger.info(f"Generated {len(chunks)} chunks.")

    # 4. LOAD (Vector DB)
    logger.info("=== Loading to Vector DB ===")
    if not settings.DATABASE_URL:
        logger.error("DATABASE_URL not set!")
        return

    # Simulate Embeddings (since we don't have OpenAI key guaranteed in env or might want to save tokens)
    # or if we have a local embedder. For now, we use dummy or check if existing.
    # The VectorDBManager expects 'embedding' field.
    
    enriched_chunks = []
    for c in chunks:
        # Dummy embedding 1536 dim
        c.embedding = [0.0] * 1536
        enriched_chunks.append(c)

    try:
        db = VectorDBManager(settings.DATABASE_URL)
        db.upsert_chunks(enriched_chunks)
        logger.info("Successfully upserted chunks to Postgres.")
    except Exception as e:
        logger.error(f"DB Upsert failed: {e}")

    logger.info("=== Pipeline Finished ===")

if __name__ == "__main__":
    run_pipeline()