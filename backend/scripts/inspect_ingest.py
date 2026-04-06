import json
import os

# Postgres
try:
    import psycopg2
except Exception as e:
    psycopg2 = None

# Redis
try:
    import redis
except Exception:
    redis = None

out = {}
# Postgres inspect
if psycopg2 is not None:
    try:
        db_url = os.getenv('DATABASE_URL')
        if db_url and '://' in db_url:
            # use psycopg2 connect string parameters
            # parse minimal from DATABASE_URL
            # fallback to explicit connection
            conn = psycopg2.connect(db_url)
        else:
            conn = psycopg2.connect("dbname=postgres user=ladiniadmin password=kingradene host=127.0.0.1 port=5433 sslmode=require")
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM ingestion.document_chunks WHERE s3_key LIKE 'raw_data/%'")
        cnt = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM ingestion.ingested_documents WHERE s3_key LIKE 'raw_data/%'")
        docs = cur.fetchone()[0]
        cur.execute("SELECT id,s3_key,chunk_index,substring(content from 1 for 200) as snippet, metadata FROM ingestion.document_chunks WHERE s3_key LIKE 'raw_data/%' ORDER BY created_at DESC LIMIT 3")
        rows = cur.fetchall()
        samples = []
        for r in rows:
            samples.append({'id':str(r[0]), 's3_key':r[1], 'chunk_index':r[2], 'snippet': (r[3] or '')[:200], 'metadata': r[4]})
        out['postgres'] = {'chunk_count': cnt, 'ingested_documents': docs, 'samples': samples}
        cur.close()
        conn.close()
    except Exception as e:
        out['postgres_error'] = str(e)
else:
    out['postgres_error'] = 'psycopg2 not installed'

# Redis inspect
if redis is not None:
    try:
        r = redis.Redis(host='127.0.0.1', port=6379, ssl=True, ssl_cert_reqs=None, decode_responses=False)
        total = r.scard('rag:{docs}:ids')
        sample = r.srandmember('rag:{docs}:ids')
        if sample:
            sid = sample.decode() if isinstance(sample, bytes) else str(sample)
            key = f"rag:{{docs}}:doc:{sid}"
            text = r.hget(key, 'text')
            meta = r.hget(key, 'meta')
            text_snip = text.decode('utf-8', errors='ignore')[:200] if text else ''
            meta_obj = None
            if meta:
                try:
                    meta_obj = json.loads(meta.decode('utf-8'))
                except Exception:
                    meta_obj = str(meta)
            out['redis'] = {'total_ids': total, 'sample_id': sid, 'text_snippet': text_snip, 'meta': meta_obj}
        else:
            out['redis'] = {'total_ids': total, 'sample_id': None}
    except Exception as e:
        out['redis_error'] = str(e)
else:
    out['redis_error'] = 'redis package not installed'

print(json.dumps(out, ensure_ascii=False, indent=2))
