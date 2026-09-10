import redis

u='rediss://localhost:6380'
try:
    r=redis.from_url(u, socket_timeout=5, decode_responses=True)
    key='rag:{docs}:ids'
    count = r.scard(key)
    print('key', key, 'count', count)
    ids = r.srandmember(key, 10)
    print('sample_ids', ids)
except Exception as e:
    print('error', e)
