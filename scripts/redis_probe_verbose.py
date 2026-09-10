#!/usr/bin/env python3
import traceback
import json
import socket
from urllib.parse import urlparse

OUT = {}
url = 'redis://127.0.0.1:6380/0'
OUT['used_url'] = url

# socket-level reachability
try:
    parsed = urlparse(url)
    h = parsed.hostname or '127.0.0.1'
    p = int(parsed.port or 6379)
    s = socket.socket()
    s.settimeout(3.0)
    s.connect((h, p))
    s.close()
    OUT['socket_connect'] = 'ok'
except Exception as e:
    OUT['socket_connect'] = f'error:{e}'

# Try redis.from_url (no TLS)
try:
    import redis
    r = redis.from_url(url, socket_timeout=4)
    try:
        OUT['from_url_ping_no_tls'] = r.ping()
    except Exception as e:
        OUT['from_url_ping_no_tls_error'] = traceback.format_exc()
except Exception as e:
    OUT['from_url_no_tls_error'] = traceback.format_exc()

# Try redis.Redis with TLS enabled (some tunnels require TLS to remote)
try:
    import redis
    r2 = redis.Redis(host='127.0.0.1', port=6380, ssl=True, ssl_check_hostname=False, ssl_cert_reqs=None, socket_timeout=4)
    try:
        OUT['redis_ssl_ping'] = r2.ping()
    except Exception as e:
        OUT['redis_ssl_ping_error'] = traceback.format_exc()
    # probe RediSearch commands
    try:
        OUT['FT.INFO'] = r2.execute_command('FT.INFO', 'rag:index')
    except Exception as e2:
        OUT['FT.INFO.error'] = str(e2)
    try:
        OUT['FT.SEARCH'] = r2.execute_command('FT.SEARCH', 'rag:index', 'prix')
    except Exception as e3:
        OUT['FT.SEARCH.error'] = str(e3)
    try:
        OUT['MODULE.LIST'] = r2.execute_command('MODULE', 'LIST')
    except Exception as e4:
        OUT['MODULE.LIST.error'] = str(e4)
except Exception as e:
    OUT['redis_ssl_error'] = traceback.format_exc()

# Try to instantiate RedisSearchProvider with verbose catching
try:
    from agriconnect.rag.providers.redis_search_provider import RedisSearchProvider
    try:
        prov = RedisSearchProvider(url, dim=384, ensure_index=False, socket_timeout=4, decode_responses=True)
        OUT['provider_init'] = 'ok'
        try:
            OUT['provider_health'] = prov.health()
        except Exception as eh:
            OUT['provider_health_error'] = traceback.format_exc()
    except Exception as eprov:
        OUT['provider_init_error'] = traceback.format_exc()
except Exception as eimp:
    OUT['provider_import_error'] = traceback.format_exc()

print(json.dumps(OUT, ensure_ascii=False))
