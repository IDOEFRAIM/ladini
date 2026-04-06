#!/usr/bin/env python3
from __future__ import annotations
import os, json, sys
from pathlib import Path
# load backend/.env if present
root = Path(__file__).resolve().parents[1]
env_path = root / '.env'
if env_path.exists():
    for line in env_path.read_text(encoding='utf-8').splitlines():
        line=line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        k,v = line.split('=',1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# allow passing s3 uri as arg
s3_uri = None
if len(sys.argv) > 1:
    s3_uri = sys.argv[1]
else:
    # default from last known upload
    s3_uri = 's3://ladini-storage/redis_ingest_output_20260321T190240Z.json'

if not s3_uri.startswith('s3://'):
    print(json.dumps({'error':'s3 uri must start with s3://'}))
    sys.exit(2)

bucket_key = s3_uri[5:].split('/',1)
if len(bucket_key) != 2:
    print(json.dumps({'error':'s3 uri must be s3://bucket/key'}))
    sys.exit(2)
bucket, key = bucket_key[0], bucket_key[1]

# download with boto3
try:
    import boto3
    from botocore.exceptions import ClientError
except Exception:
    boto3 = None

s3_ok = False
entries_count = 0
weather_advisory_count = 0
sample_meta = []
local_tmp = root / Path(key).name
if boto3 is None:
    print(json.dumps({'error':'boto3 not available in environment'}))
    sys.exit(3)

try:
    s3 = boto3.client('s3', region_name=os.getenv('S3_REGION') or None)
    resp = s3.get_object(Bucket=bucket, Key=key)
    body = resp['Body'].read()
    # attempt to decode and parse JSON
    try:
        data = json.loads(body)
    except Exception:
        # maybe lines of json
        text = body.decode('utf-8', errors='replace')
        try:
            data = json.loads(text)
        except Exception:
            # try json lines
            data = []
            for line in text.splitlines():
                line=line.strip()
                if not line:
                    continue
                try:
                    data.append(json.loads(line))
                except Exception:
                    pass
    entries_count = len(data) if isinstance(data, list) else 1
    if isinstance(data, list):
        for i,entry in enumerate(data):
            md = None
            if isinstance(entry, dict):
                md = entry.get('metadata') or entry.get('meta') or entry.get('metadata') or entry.get('doc_type')
                # check doc_type in metadata or top-level keys
                if isinstance(entry.get('metadata'), dict) and entry.get('metadata').get('doc_type') == 'weather_advisory':
                    weather_advisory_count += 1
                if entry.get('doc_type') == 'weather_advisory':
                    weather_advisory_count += 1
                if i < 5:
                    sample_meta.append({
                        'id': entry.get('id') or entry.get('chunk_id') or None,
                        'doc_type': (entry.get('metadata') or {}).get('doc_type') if isinstance(entry.get('metadata'), dict) else entry.get('doc_type')
                    })
    s3_ok = True
except ClientError as e:
    print(json.dumps({'error':'s3 get_object failed','detail': str(e)}))
    sys.exit(4)
except Exception as e:
    print(json.dumps({'error':'s3 download failed','detail': str(e)}))
    sys.exit(5)

# query Postgres agri_weather.observations
db_ok = False
obs_count = None
recent_obs = []
try:
    from sqlalchemy import create_engine, text
    # load settings to get DATABASE_URL if present
    try:
        import importlib
        import agriconnect.core.settings as s
        importlib.reload(s)
        db_url = os.getenv('DATABASE_URL') or getattr(s, 'settings', None) and getattr(s.settings, 'DATABASE_URL', None)
        if not db_url:
            db_url = os.getenv('DATABASE_URL') or os.getenv('AGRI_DATABASE_URL')
    except Exception:
        db_url = os.getenv('DATABASE_URL')

    if db_url:
        engine = create_engine(db_url)
        with engine.begin() as conn:
            r = conn.execute(text('SELECT count(*) FROM agri_weather.observations'))
            obs_count = int(r.scalar() or 0)
            r2 = conn.execute(text('SELECT zone_name, observed_at, temperature_c, precipitation_mm FROM agri_weather.observations ORDER BY observed_at DESC LIMIT 10'))
            for row in r2:
                recent_obs.append({
                    'zone_name': row[0], 'observed_at': str(row[1]), 'temperature_c': row[2], 'precipitation_mm': row[3]
                })
        db_ok = True
    else:
        db_ok = False
except Exception as e:
    db_ok = False
    db_err = str(e)

out = {
    's3': {'ok': s3_ok, 'bucket': bucket, 'key': key, 'entries_count': entries_count, 'weather_advisory_count': weather_advisory_count, 'sample': sample_meta},
    'db': {'ok': db_ok, 'observations_count': obs_count, 'recent': recent_obs}
}
print(json.dumps(out, ensure_ascii=False, indent=2))

if not s3_ok or not db_ok:
    sys.exit(10 if not s3_ok else 11)
else:
    sys.exit(0)
