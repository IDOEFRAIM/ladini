import os
import psycopg2

# Load env from backend/.env if present
env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
if os.path.exists(env_path):
    with open(env_path, encoding='utf-8') as f:
        for line in f.read().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

DB = os.environ.get('DATABASE_URL')
if not DB:
    print('DATABASE_URL not set')
    raise SystemExit(1)

conn = psycopg2.connect(DB)
cur = conn.cursor()
try:
    cur.execute("SELECT count(*) FROM agri_vector.document_chunks;")
    row = cur.fetchone()
    print('document_chunks count:', row[0] if row else 0)
except Exception as e:
    print('Error querying agri_vector.document_chunks:', e)
finally:
    cur.close()
    conn.close()
