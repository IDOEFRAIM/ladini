import sys
import os
from pathlib import Path

# Fix path to include backend/src
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agriconnect.core.settings import settings
import psycopg2
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("VersionMigration")

def migrate_versioning_and_hnsw():
    db_url = settings.DATABASE_URL
    if not db_url:
        logger.error("DATABASE_URL not configured")
        return

    conn = psycopg2.connect(db_url)
    conn.autocommit = True  # Important for creating indexes concurrently
    cur = conn.cursor()

    try:
        # 1. Add embedding_model column if not exists
        logger.info("Adding embedding_model column...")
        cur.execute("""
            ALTER TABLE agri_vector.document_chunks 
            ADD COLUMN IF NOT EXISTS embedding_model VARCHAR(50) DEFAULT 'text-embedding-3-small';
        """)

        # 2. Add embedding_dimensions column if not exists
        logger.info("Adding embedding_dimensions column...")
        cur.execute("""
            ALTER TABLE agri_vector.document_chunks 
            ADD COLUMN IF NOT EXISTS embedding_dimensions INT DEFAULT 1536;
        """)
        
        # 3. Create HNSW Index (if not exists logic is tricky in raw sql, usually we catch error or check first)
        # Using vector_cosine_ops for cosine similarity
        logger.info("Creating HNSW index (this might take a while)...")
        try:
            # We use IF NOT EXISTS for PG15+, but for older versions we might need a block
            # Assuming PG15+ for pgvector
            cur.execute("""
                CREATE INDEX IF NOT EXISTS document_chunks_embedding_hnsw_idx 
                ON agri_vector.document_chunks 
                USING hnsw (embedding vector_cosine_ops);
            """)
            logger.info("HNSW Index created successfully.")
        except Exception as e:
            logger.warning(f"Index creation notice: {e}")

        logger.info("Migration completed successfully.")

    except Exception as e:
        logger.error(f"Migration failed: {e}")
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    migrate_versioning_and_hnsw()