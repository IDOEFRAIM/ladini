import redis

urls = ('redis://localhost:6380','rediss://localhost:6380')
for url in urls:
    try:
        r = redis.from_url(url, socket_connect_timeout=5, socket_timeout=5)
        print(url + ' ->', r.ping())
    except Exception as e:
        print(url + ' ->', repr(e))
