import sys
import os
from pathlib import Path
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

# Encoding hack
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except:
        pass

try:
    from agriconnect.core.settings import settings
except ImportError as e:
    with open("error.txt", "w") as f:
        f.write(f"ImportError: {e}")
    sys.exit(1)

DB_URL = str(settings.DATABASE_URL)

try:
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    cur = conn.cursor()

    # Dimensions
    cur.execute("SELECT atttypmod FROM pg_attribute WHERE attrelid = 'agri_vector.document_chunks'::regclass AND attname = 'embedding';")
    print(f"Dims: {cur.fetchone()}")

    # Drop doc_ref
    try:
        cur.execute("ALTER TABLE agri_vector.document_chunks DROP COLUMN IF EXISTS doc_ref CASCADE;")
    except Exception as e:
        print(f"Drop failed: {e}")

    # Ensure unique constraint
    try:
        cur.execute("ALTER TABLE agri_vector.document_chunks ADD CONSTRAINT document_chunks_content_hash_key UNIQUE (content_hash);")
    except Exception as e:
        print(f"Constraint failed (exists?): {e}")

    # Success marker
    with open("success.txt", "w") as f:
        f.write("SCHEMA FIXED SUCCESSFULLY")

    cur.close()
    conn.close()

except Exception as e:
    with open("error.txt", "w") as f:
        f.write(f"DB Error: {e}")
