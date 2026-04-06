import os
import psycopg2
import json

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
    cur.execute("select column_name, data_type from information_schema.columns where table_schema='agri_vector' and table_name='document_chunks';")
    cols = cur.fetchall()
    print('columns:', json.dumps(cols, ensure_ascii=False))

    cur.execute("SELECT count(*) FROM agri_vector.document_chunks;")
    print('count:', cur.fetchone()[0])

    cur.execute("SELECT column_name, constraint_type FROM information_schema.key_column_usage k JOIN information_schema.table_constraints t ON k.constraint_name=t.constraint_name WHERE k.table_schema='agri_vector' AND k.table_name='document_chunks';")
    cons = cur.fetchall()
    print('constraints:', json.dumps(cons, ensure_ascii=False))

    cur.execute("SELECT * FROM agri_vector.legacy_rag_import_log ORDER BY created_at DESC LIMIT 5;")
    logs = cur.fetchall()
    print('recent_logs_count:', len(logs))
    for r in logs:
        try:
            print('log:', r)
        except Exception:
            pass

except Exception as e:
    print('error:', e)
finally:
    cur.close()
    conn.close()
