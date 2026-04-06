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
        print("CRITICAL: 'doc_ref' column STILL EXISTS!")
    else:
        print("OK: 'doc_ref' column is gone.")

    # Check dimensions
    cur.execute("""
        SELECT atttypmod 
        FROM pg_attribute 
        WHERE attrelid = 'agri_vector.document_chunks'::regclass 
        AND attname = 'embedding';
    """)
    res = cur.fetchone()
    print(f"Embedding dimensions: {res[0] if res else 'Not found'}")
    
    conn.close()

except Exception as e:
    print(f"Error: {e}")
