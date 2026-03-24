"""
Event-Driven Ingestion Worker.
Monitors configured paths/S3 prefixes for new files and processes them.
"""

import os
import sys
import time
import logging
from pathlib import Path
from typing import List, Optional

from tqdm import tqdm

from agriconnect.rag.components import get_embedding_model

from backend.ingestion.processors.factory import get_processor
from backend.ingestion.storage.vector_db import VectorDBManager
from backend.ingestion.schema import RawDocument, DocumentChunk

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

    def __init__(self, raw_data_dir: str):
        self.raw_data_dir = Path(raw_data_dir)
        self.vector_db = VectorDBManager()
        self.embedding_model = get_embedding_model() # LlamaIndex EmbedModel
        
        if not self.raw_data_dir.exists():
            logger.warning(f"Raw data directory {self.raw_data_dir} does not exist.")
            # For S3, we would check bucket existence.
            pass

    def run(self, continuous: bool = False):
        """Runs the worker loop."""
        logger.info(f"Starting Ingestion Worker on {self.raw_data_dir}")
        
        while True:
            try:
                # 1. Scan for new files
                new_files = self._scan_files()
                
                if not new_files:
                    if not continuous:
                        break
                    time.sleep(10) # Poll every 10s
                    continue

                logger.info(f"Found {len(new_files)} files to process.")
                
                for file_path in tqdm(new_files, desc="Ingesting"):
                    try:
                        self.process_file(file_path)
                    except Exception as e:
                        logger.error(f"Failed to process {file_path}: {e}")
                
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

    def _scan_files(self) -> List[Path]:
        """Simple file scan. In v2, use Redis Stream for events."""
        # For now, just list all relevant files.
        # Ideally, we track processed state in a DB (like legacy ingestor did).
        # We can implement a lightweight processed_files.log for this MVP.
        
        processed_log = Path("processed_files.log")
        processed_files = set()
        if processed_log.exists():
            with open(processed_log, "r") as f:
                processed_files = set(f.read().splitlines())

        candidates = []
        for ext in ["*.pdf", "*.json", "*.txt"]:
            candidates.extend(self.raw_data_dir.rglob(ext))
            
        new_files = [f for f in candidates if str(f) not in processed_files]
        return new_files

    def _mark_processed(self, file_path: Path):
        with open("processed_files.log", "a") as f:
            f.write(f"{file_path}\n")

    def _detect_type(self, file_path: Path) -> str:
        """Heuristic to determine processor type."""
        fp_str = str(file_path).lower()
        if "fews" in fp_str or "report" in fp_str:
            return "fews_report"
        if "news" in fp_str:
            return "news_article"
        if "weather" in fp_str or "bulletin" in fp_str:
            return "weather_bulletin"
        if file_path.suffix == ".pdf":
            return "pdf"
        if file_path.suffix == ".json":
            # inspect json structure if needed
            with open(file_path, "r") as f:
                snippet = f.read(200)
                if "forecast" in snippet: return "weather_bulletin"
            return "technical_resource" # fallback
        return "technical_resource"

    def process_file(self, file_path: Path):
        """Processes a single file end-to-end."""
        logger.info(f"Processing: {file_path}")
        
        # 1. Load Content (Simple read)
        # TODO: Use specialized loaders (e.g. PyPDF) if needed, 
        # but for now assume text extraction is mostly handled or simple.
        # Actually, for PDF we need extraction. 
        # Let's use LlamaIndex SimpleDirectoryReader just for extraction on the fly.
        
        from llama_index.core import SimpleDirectoryReader
        docs = SimpleDirectoryReader(input_files=[str(file_path)]).load_data()
        
        if not docs:
            logger.warning(f"No content extracted from {file_path}")
            return

        # Merge pages into one RawDocument for processing context
        full_text = "\n\n".join([d.text for d in docs])
        
        source_type = self._detect_type(file_path)
        
        raw_doc = RawDocument(
            source_id=str(file_path),
            source_type=source_type,
            content=full_text,
            title=file_path.name,
            metadata={"filename": file_path.name, "path": str(file_path)}
        )

        # 2. Get Processor
        processor = get_processor(source_type)
        
        # 3. Clean & Chunk
        cleaned_doc = processor.clean(raw_doc)
        chunks = processor.chunk(cleaned_doc)
        
        if not chunks:
            logger.warning(f"No chunks generated for {file_path}")
            return

        # 4. Embed Chunks
        # Batch embedding
        texts = [c.text_content for c in chunks]
        embeddings = self.embedding_model.get_text_embedding_batch(texts)
        
        for i, chunk in enumerate(chunks):
            chunk.embedding = embeddings[i]

        # 5. Upsert to VectorDB
        self.vector_db.upsert_chunks(chunks)
        
        # 6. Mark done
        self._mark_processed(file_path)
        logger.info(f"Successfully processed {file_path} ({len(chunks)} chunks)")


if __name__ == "__main__":
    # Import settings to get default RAW_DATA_DIR
    from agriconnect.rag.config import RAW_DATA_DIR
    
    worker = IngestionWorker(raw_data_dir=str(RAW_DATA_DIR))
    
    # Run once by default, use --watch for continuous
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true", help="Run in continuous watch mode")
    args = parser.parse_args()
    
    worker.run(continuous=args.watch)
