"""Patch vector dimensions using agriconnect settings configuration."""
import sys
from pathlib import Path
# Insert backend/src into path to mimic ensure_db_prereqs.py style
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

try:
    from agriconnect.core.settings import settings
except ImportError as e:
    print(f"Failed to import settings: {e}")
    sys.exit(1)

import psycopg2
import sys

# Windows encoding hack
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except:
        pass

DB_URL = str(settings.DATABASE_URL)
try:
    print(f'Using DB_URL: {DB_URL}')
except:
    pass

try:
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    cur = conn.cursor()
    
    cur.execute("SELECT current_database();")
    try:
        print(f"Connected to database: {cur.fetchone()[0]}")
    except:
        pass

    # Check current schema
    cur.execute("""
        SELECT atttypmod 
        FROM pg_attribute 
        WHERE attrelid = 'agri_vector.document_chunks'::regclass 
        AND attname = 'embedding';
    """)
    res = cur.fetchone()
    print(f"Current embedding modifier: {res[0] if res else 'None'}", flush=True)

    # 1. Truncate table
    print("Truncating table 'agri_vector.document_chunks'...", flush=True)
    try:
        cur.execute("TRUNCATE TABLE agri_vector.document_chunks CASCADE;")
        print("Table truncated.")
    except Exception as e:
        print(f"Error truncating: {e}")

    # 2. Alter column to vector(1536)
    print("Altering column 'embedding' to vector(1536)...", flush=True)
    try:
        # Drop old column first to ensure clean state
        cur.execute("ALTER TABLE agri_vector.document_chunks DROP COLUMN IF EXISTS embedding;")
        cur.execute("ALTER TABLE agri_vector.document_chunks ADD COLUMN embedding vector(1536);")
        print("Successfully rebooted column to vector(1536).")
    except Exception as e:
        print(f"Error altering column: {e}")

    # Check new schema
    cur.execute("""
        SELECT atttypmod 
        FROM pg_attribute 
        WHERE attrelid = 'agri_vector.document_chunks'::regclass 
        AND attname = 'embedding';
    """)
    res = cur.fetchone()
    print(f"New embedding modifier: {res[0] if res else 'None'}", flush=True)

    cur.close()
    conn.close()
    print("Schema dimension patch COMPLETED SUCCESSFULLY.")

except Exception as e:
    try:
        print(f"Connection failed: {e}")
    except:
        print("Connection failed (output encoding error)")
