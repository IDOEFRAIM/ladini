"""Drop legacy 'doc_ref' column from document_chunks."""
import sys
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

    print("Dropping legacy column 'doc_ref'...")
    try:
        cur.execute("ALTER TABLE agri_vector.document_chunks DROP COLUMN IF EXISTS doc_ref;")
        print("Column dropped.")
    except Exception as e:
        print(f"Error dropping column: {e}")

    cur.close()
    conn.close()
    print("Legacy column cleanup COMPLETED.")

except Exception as e:
    try:
        print(f"Execution failed: {e}")
    except:
        print("Execution failed (encoding error)")
