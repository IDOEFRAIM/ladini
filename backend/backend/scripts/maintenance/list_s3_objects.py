#!/usr/bin/env python3
import boto3
import sys
import os
from pathlib import Path

BUCKET = 'ladini-storage'
PREFIX = 'raw_data/'


def _load_dotenv_like(path: Path):
    if not path.exists():
        return
    for ln in path.read_text(encoding='utf-8').splitlines():
        ln = ln.strip()
        if not ln or ln.startswith('#'):
            continue
        if '=' not in ln:
            continue
        k, v = ln.split('=', 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def main():
    # try to load backend/.env for credentials if present
    env_path = Path(__file__).resolve().parents[0] / '..' / '.env'
    _load_dotenv_like(env_path)

    s3 = boto3.client('s3')
    kwargs = {'Bucket': BUCKET, 'Prefix': PREFIX}
    while True:
        resp = s3.list_objects_v2(**kwargs)
        for o in resp.get('Contents', []):
            print(o['Key'])
        if not resp.get('IsTruncated'):
            break
        kwargs['ContinuationToken'] = resp.get('NextContinuationToken')


if __name__ == '__main__':
    if len(sys.argv) > 1:
        PREFIX = sys.argv[1]
    main()
