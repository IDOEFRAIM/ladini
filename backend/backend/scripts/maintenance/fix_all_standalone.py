import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

# HARDCODED FOR RELIABILITY
DB_HOST = "localhost"
DB_NAME = "agriconnect"
DB_USER = "postgres"
DB_PASSWORD = "postgres" # Check your .env if different
DB_PORT = "5432"

DSN = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

print(f"Connecting to {DSN}...", flush=True)

try:
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()

    # Drop doc_ref
    print("Dropping doc_ref...", flush=True)
    try:
        cur.execute("ALTER TABLE agri_vector.document_chunks DROP COLUMN IF EXISTS doc_ref CASCADE;")
        print("Dropped doc_ref.")
    except Exception as e:
        print(f"Error dropping doc_ref: {e}")

    # Add constraint
    print("Adding constraint...", flush=True)
    try:
        cur.execute("ALTER TABLE agri_vector.document_chunks ADD CONSTRAINT document_chunks_content_hash_key UNIQUE (content_hash);")
        print("Done.")
    except Exception as e:
        print(f"Constraint error (likely exists): {e}")

    # Verify dimensions for good measure
    print("Verifying dimensions...", flush=True)
    cur.execute("SELECT atttypmod FROM pg_attribute WHERE attrelid = 'agri_vector.document_chunks'::regclass AND attname = 'embedding';")
    print(f"Dims: {cur.fetchone()}")

    cur.close()
    conn.close()
    print("FIX COMPLETED.")

except Exception as e:
    print(f"Connection failed: {e}")
