import os
import sys
from dotenv import load_dotenv, find_dotenv
import psycopg2
from agriconnect.core.settings import settings

# Load .env explicitly
dotenv_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "agriconnect", ".env"))
load_dotenv(dotenv_path)

def patch():
    db_url = str(settings.DATABASE_URL)
    print(f"DB URL: {db_url}")
    conn = None
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        
        # Ensure schema exists
        cur.execute("CREATE SCHEMA IF NOT EXISTS agri_vector;")
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        
        # Check if table exists, if not create fully
        try:
             cur.execute("SELECT 1 FROM agri_vector.document_chunks LIMIT 1;")
        except psycopg2.errors.UndefinedTable:
             conn.rollback()
             print("Table does not exist. Creating...")
             try:
                 cur.execute("""
                    CREATE TABLE agri_vector.document_chunks (
                        chunk_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        parent_doc_source_id TEXT,
                        chunk_index INTEGER DEFAULT 0,
                        text_content TEXT,
                        content_hash TEXT UNIQUE,
                        embedding vector(1536),
                        metadata JSONB,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                 """)
                 conn.commit()
                 print("Table created.")
                 return
             except Exception as e2:
                 print(f"Failed to create table: {e2}")
                 conn.rollback()
                 return
        
        # If exists, add missing columns
        columns = [
            ("parent_doc_source_id", "TEXT"),
            ("chunk_index", "INTEGER DEFAULT 0"),
            ("text_content", "TEXT"),
            ("content_hash", "TEXT"),
            ("embedding", "vector(1536)"),
            ("metadata", "JSONB"),
            ("created_at", "TIMESTAMPTZ DEFAULT NOW()")
        ]

        for col, dtype in columns:
            print(f"Checking {col}...")
            try:
                cur.execute(f"ALTER TABLE agri_vector.document_chunks ADD COLUMN IF NOT EXISTS {col} {dtype};")
                conn.commit()
            except Exception as e:
                print(f"Error checking {col}: {e}")
                conn.rollback()

        print("Schema patched successfully.")
    except Exception as e:
        print(f"Error patching schema: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    patch()