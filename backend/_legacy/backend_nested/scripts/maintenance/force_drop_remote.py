import sys
from pathlib import Path
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

try:
    from agriconnect.core.settings import settings
except ImportError:
    print("Settings import failed")
    sys.exit(1)

# Windows encoding hack
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except:
        pass

print(f"Connecting to: {settings.DATABASE_URL}", flush=True)

try:
    conn = psycopg2.connect(str(settings.DATABASE_URL))
    conn.autocommit = True
    cur = conn.cursor()
    
    # Check doc_ref
    cur.execute("""
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_schema = 'agri_vector' 
        AND table_name = 'document_chunks' 
        AND column_name = 'doc_ref';
    """)
    if cur.fetchone():
        print("Found 'doc_ref', attempting to drop...", flush=True)
        try:
            cur.execute("ALTER TABLE agri_vector.document_chunks DROP COLUMN doc_ref CASCADE;")
            print("Dropped 'doc_ref' successfully.", flush=True)
        except Exception as e:
            print(f"Error dropping 'doc_ref': {e}", flush=True)
    else:
        print("'doc_ref' does not exist.", flush=True)

    conn.close()

except Exception as e:
    print(f"Error: {e}")
