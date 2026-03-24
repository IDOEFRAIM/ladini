import sys
import os
from pathlib import Path

# Fix path to include backend/src
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agriconnect.core.settings import settings
import psycopg2

def inspect_vector_schema():
    db_url = settings.DATABASE_URL
    if not db_url:
        print("ERROR: DATABASE_URL not found.")
        return

    print(f"Connecting to DB...")
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        
        # Check columns in document_chunks
        print("\n--- Columns in agri_vector.document_chunks ---")
        cur.execute("""
            SELECT column_name, data_type, udt_name
            FROM information_schema.columns 
            WHERE table_schema = 'agri_vector' 
              AND table_name = 'document_chunks'
            ORDER BY ordinal_position;
        """)
        columns = cur.fetchall()
        for col in columns:
            print(f"- {col[0]}: {col[1]} ({col[2]})")
            
            # If embedding, check dimensions
            if col[0] == 'embedding':
                # Use a trick to get dimensions if vector type
                # But information_schema might just say 'USER-DEFINED'
                pass

        # Check actual vector dimensions by querying one row (or metadata if empty)
        # Or inspect pg_attribute
        print("\n--- Checking embedding dimensions via pg_attribute ---")
        cur.execute("""
            SELECT atttypmod 
            FROM pg_attribute 
            WHERE attrelid = 'agri_vector.document_chunks'::regclass 
              AND attname = 'embedding';
        """)
        row = cur.fetchone()
        if row:
            dims = row[0]
            print(f"Embedding dimensions (atttypmod): {dims}")
        else:
            print("Could not find embedding column in pg_attribute.")

        cur.close()
        conn.close()
        print("\nSchema inspection complete.")
        
    except Exception as e:
        print(f"Error inspecting DB: {e}")

if __name__ == "__main__":
    inspect_vector_schema()
