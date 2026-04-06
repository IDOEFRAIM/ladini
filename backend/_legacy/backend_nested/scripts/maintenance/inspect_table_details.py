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
    print('--- Indexes ---')
    cur.execute("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='agri_vector' AND tablename='document_chunks';")
    for r in cur.fetchall():
        print(r)

    print('\n--- Triggers ---')
    cur.execute("SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger WHERE tgrelid = 'agri_vector.document_chunks'::regclass;")
    for r in cur.fetchall():
        print(r)

    print('\n--- Rules ---')
    cur.execute("SELECT * FROM pg_rules WHERE schemaname='agri_vector' AND tablename='document_chunks';")
    for r in cur.fetchall():
        print(r)

    print('\n--- Table DDL (partial) ---')
    cur.execute("SELECT pg_get_tabledef('agri_vector.document_chunks'::regclass)")
    try:
        print(cur.fetchone())
    except Exception:
        pass

except Exception as e:
    print('error:', e)
finally:
    cur.close()
    conn.close()
