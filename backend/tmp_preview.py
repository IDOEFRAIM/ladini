import os, boto3, pathlib, sys, traceback
p = pathlib.Path(__file__).resolve().parent/'backend' / '.env'
# if path constructed wrong (we are in backend), try parent
if not p.exists():
    p = pathlib.Path(__file__).resolve().parent/ '.env'
if p.exists():
    for line in p.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k,v = line.split('=',1)
        k=k.strip(); v=v.strip().strip('"').strip("'")
        if os.environ.get(k) is None:
            os.environ[k]=v

bucket = 'ladini-storage'
key = 'raw_data/redis_ingest_output_20260320T184009Z.json'
if len(sys.argv) > 1:
    key = sys.argv[1]
if len(sys.argv) > 2:
    bucket = sys.argv[2]

try:
    s3 = boto3.client('s3')
    obj = s3.get_object(Bucket=bucket, Key=key)
    data = obj['Body'].read(2000000)
    text = data.decode('utf-8','replace')
    print(text[:4000])
except Exception:
    traceback.print_exc()
