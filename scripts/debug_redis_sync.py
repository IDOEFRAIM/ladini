#!/usr/bin/env python3
import os, json, traceback
from urllib.parse import urlparse

REDIS_URL = os.getenv('AGRICONNECT_REDIS_URL') or os.getenv('REDIS_URL') or 'rediss://127.0.0.1:6380/0'
PREFIXES = [os.getenv('INGESTION_DOC_PREFIX','doc:'), 'doc:', 'rag:doc:', 'rag:']

out = {'used_url': REDIS_URL, 'module_list': None, 'sample_keys': [], 'scan_matches': [], 'ft_info': None}

try:
    import redis
    parsed = urlparse(REDIS_URL)
    host = parsed.hostname or '127.0.0.1'
    port = int(parsed.port or 6379)
    ssl = parsed.scheme == 'rediss'
    client = redis.Redis(host=host, port=port, ssl=ssl, ssl_check_hostname=False, ssl_cert_reqs=None, socket_timeout=5)
    try:
        out['ping'] = client.ping()
    except Exception as e:
        out['ping_error'] = str(e)

    # MODULE LIST
    try:
        mod = client.execute_command('MODULE', 'LIST')
        out['module_list'] = mod
    except Exception as e:
        out['module_list_error'] = str(e)

    # FT.INFO check
    try:
        ft = client.execute_command('FT.INFO', 'rag:index')
        out['ft_info'] = ft
    except Exception as e:
        out['ft_info_error'] = str(e)

    # SCAN for ingestion prefixes
    try:
        scanned = []
        for prefix in PREFIXES:
            it = client.scan_iter(match=prefix + '*', count=1000)
            found = []
            for i, k in enumerate(it):
                if i >= 20:
                    break
                try:
                    found.append(k.decode('utf-8') if isinstance(k, (bytes,bytearray)) else str(k))
                except Exception:
                    found.append(str(k))
            if found:
                out['scan_matches'].append({'prefix': prefix, 'examples': found})
    except Exception as e:
        out['scan_error'] = str(e)

except Exception as e:
    out['connect_error'] = traceback.format_exc()

print(json.dumps(out, ensure_ascii=False, indent=2))
