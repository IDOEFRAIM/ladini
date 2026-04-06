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
    vec = '[' + ','.join(['0.0']*384) + ']'
    sql = '''
    INSERT INTO agri_vector.document_chunks (
        doc_ref, doc_type, category, zone_name, content, content_md5,
        embedding, forecast_date, valid_until, metadata, source_uri, chunk_version, is_active
    ) VALUES (
        %s,%s,%s,%s,%s,%s, %s::vector, %s,%s,%s,%s,1,TRUE
    ) RETURNING chunk_id
    '''
    params = (
        'test-emb-doc', 'test', None, None, 'content emb test', 'md5embtest', vec, None, None, '{}', 'tests'
    )
    cur.execute(sql, params)
    print('status:', getattr(cur, 'statusmessage', None))
    row = cur.fetchone()
    conn.commit()
    print('inserted id:', row)
except Exception as e:
    print('error:', e)
    conn.rollback()
finally:
    cur.close()
    conn.close()
