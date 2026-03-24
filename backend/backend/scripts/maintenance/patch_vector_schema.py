import os
import sys
from dotenv import load_dotenv, find_dotenv

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

# Load .env explicitly
dotenv_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "agriconnect", ".env"))
print(f"Loading .env from {dotenv_path}")
load_dotenv(dotenv_path)

from agriconnect.core.settings import settings
import psycopg2

def patch():
    db_url = str(settings.DATABASE_URL)
    print(f"DB URL: {db_url}")
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        
        print("Adding parent_doc_source_id column...")
        cur.execute("ALTER TABLE agri_vector.document_chunks ADD COLUMN IF NOT EXISTS parent_doc_source_id TEXT;")
        
        print("Adding chunk_index column...") 
        cur.execute("ALTER TABLE agri_vector.document_chunks ADD COLUMN IF NOT EXISTS chunk_index INTEGER DEFAULT 0;")

        conn.commit()
        print("Schema patched successfully.")
    except Exception as e:
        print(f"Error patching schema: {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

if __name__ == "__main__":
    patch()