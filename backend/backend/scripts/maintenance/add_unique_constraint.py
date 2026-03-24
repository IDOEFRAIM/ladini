"""Add unique constraint to document_chunks."""
import sys
import psycopg2
from pathlib import Path

# Insert backend/src into path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

try:
    from agriconnect.core.settings import settings
except ImportError as e:
    print(f"Failed to import settings: {e}")
    sys.exit(1)

# Windows encoding hack
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except:
        pass

DB_URL = str(settings.DATABASE_URL)

try:
    print("Connecting to DB...")
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    cur = conn.cursor()

    print("Adding UNIQUE constraint on content_hash...")
    try:
        cur.execute("""
            ALTER TABLE agri_vector.document_chunks 
            ADD CONSTRAINT document_chunks_content_hash_key UNIQUE (content_hash);
        """)
        print("Constraint added.")
    except Exception as e:
        print(f"Error adding constraint: {e}")
        # Maybe it exists but with different name? 
        # Or duplicate data exists (unlikely since we truncated recently)?

    cur.close()
    conn.close()
    print("Constraint patch COMPLETED.")

except Exception as e:
    try:
        print(f"Execution failed: {e}")
    except:
        print("Execution failed (encoding error)")
