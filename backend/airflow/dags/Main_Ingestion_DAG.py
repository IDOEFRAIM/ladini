"""
Unified ingestion pipeline for AgriConnect.
Follows Extract -> S3 Upload -> Chunking -> Processing logic.
"""
import sys
import os
from pathlib import Path

# --- Bootstrapping Paths for Local execution & Airflow ---
def _bootstrap_path():
    # Helper to add project root to sys.path
    repo_root = Path(__file__).resolve().parents[2] # backend/
    src = repo_root / "src"
    
    # 1. Add 'backend/src' for 'agriconnect' package
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    
    # 2. Add 'backend' root for 'backend.ingestion' package
    # This allows 'from backend.ingestion...' to work
    project_root = repo_root.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

_bootstrap_path()
# -----------------------------------------------------

from airflow.decorators import dag, task
from datetime import datetime
import logging
import json
import uuid
import hashlib
from typing import List, Dict, Any

from backend.ingestion.schema import RawDocument, DocumentChunk
from backend.ingestion.storage.s3_manager import S3Manager
from backend.ingestion.storage.vector_db import VectorDBManager

from agriconnect.services.data_collection.weather.weather_collector import WeatherCollector

logger = logging.getLogger("AgriIngest")

@dag(
    dag_id="Main_Ingestion_DAG",
    schedule="@hourly",
    start_date=datetime(2026, 3, 20),
    catchup=False,
    max_active_runs=1,
    default_args={"owner": "agriconnect", "retries": 1},
    tags=["ingestion", "pipeline", "etl"]
)
def main_ingestion_pipeline():

    @task
    def fetch_sources() -> List[Dict[str, Any]]:
        """
        Runs collectors for Weather, News, FEWS, etc.
        Produces a list of RawDocument dicts for S3.
        """
        raw_docs = []
        
        # 1. Weather Collector (New Wrapper)
        try:
            from backend.ingestion.collectors.weather_collector import WeatherCollector
            wc = WeatherCollector()
            weather_docs = wc.run()
            raw_docs.extend(weather_docs)
            logger.info(f"Collected {len(weather_docs)} weather bulletins.")
        except ImportError:
            logger.warning("WeatherCollector not fully implemented or deps missing.")
        except Exception as e:
            logger.error(f"Weather collector failed: {e}")

        # 2. News Collector (New)
        try:
            from backend.ingestion.collectors.news_collector import NewsCollector
            nc = NewsCollector()
            news_docs = nc.run()
            raw_docs.extend(news_docs)
            logger.info(f"Collected {len(news_docs)} news articles.")
        except ImportError:
            logger.warning("NewsCollector not fully implemented or deps missing.")
        except Exception as e:
            logger.error(f"News collector failed: {e}")

        # 3. FEWS NET Collector (New)
        try:
            from backend.ingestion.collectors.fews_collector import FewsCollector
            fc = FewsCollector()
            fews_docs = fc.run()
            raw_docs.extend(fews_docs)
            logger.info(f"Collected {len(fews_docs)} FEWS reports.")
        except ImportError:
            logger.warning("FewsCollector not fully implemented or deps missing.")
        except Exception as e:
            logger.error(f"FEWS collector failed: {e}")

        return raw_docs

    @task
    def upload_to_s3(raw_docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Uploads standardized RawDocuments to S3 (raws_data/).
        Returns doc with raw_s3_key added.
        """
        s3 = S3Manager()
        processed = []
        for d in raw_docs:
            try:
                doc = RawDocument(**d)
                key = s3.upload_raw_json(doc, prefix="raws_data")
                doc.raw_s3_key = key
                processed.append(doc.model_dump())
            except Exception as e:
                logger.error(f"Failed to upload {d.get('source_id')}: {e}")
        return processed

    @task
    def transform_and_chunk(s3_docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Splits text content into semantic chunks using LangChain logic.
        Adds content_hash (MD5) for idempotence.
        If chunking fails, moves original S3 file to DLQ (failed/).
        """
        s3 = S3Manager()
        chunks = []
        # TODO: Import LangChain TextSplitter
        # from langchain.text_splitter import RecursiveCharacterTextSplitter
        
        for d in s3_docs:
            try:
                doc = RawDocument(**d)
                text = doc.content
                
                # Placeholder chunking logic
                # Real implementation would use RecursiveCharacterTextSplitter here
                # Chunking by 500 chars for demo
                raw_chunks = [text[i:i+500] for i in range(0, len(text), 500)]
                
                for idx, chunk_text in enumerate(raw_chunks):
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
                    chunks.append(c.model_dump())
            except Exception as e:
                logger.error(f"Chunking failed for {d.get('source_id')}: {e}")
                # DLQ Logic: Move original raw file to 'failed/' if it exists in S3
                if d.get("raw_s3_key"):
                    s3.move_to_failed(d["raw_s3_key"], reason=str(e))

        return chunks

    @task 
    def embed_and_load(chunks: List[Dict[str, Any]]):
        """
        Computes embeddings (if missing) and loads to Postgres.
        Since we abandoned Redis, this goes straight to pgvector.
        """
        from agriconnect.core.settings import settings
        
        if not chunks:
            logger.info("No chunks to process.")
            return

        # 1. Compute Embeddings (Placeholder for OpenAI call)
        # Real impl: use LangChain OpenAIEmbeddings
        enriched_chunks = []
        
        # In a real scenario, we'd batch this to OpenAI
        for c in chunks:
            chunk = DocumentChunk(**c)
            # if not chunk.embedding:
            #     chunk.embedding = openai.Embedding.create(input=chunk.text_content)
            
            # Dummy embedding for now (1536 dims for text-embedding-ada-002)
            chunk.embedding = [0.0] * 1536 
            enriched_chunks.append(chunk)

        # 2. Load to Postgres
        # We need a valid DB URL from settings
        db_url = settings.DATABASE_URL
        if db_url:
            db = VectorDBManager(db_url)
            db.upsert_chunks(enriched_chunks)
            logger.info(f"Upserted {len(enriched_chunks)} chunks to Postgres.")
        else:
            logger.error("No DATABASE_URL configured.")

    # DAG Flow
    raw_data = fetch_sources()
    s3_refs = upload_to_s3(raw_data)
    chunk_batch = transform_and_chunk(s3_refs)
    embed_and_load(chunk_batch)

main_ingestion_pipeline()