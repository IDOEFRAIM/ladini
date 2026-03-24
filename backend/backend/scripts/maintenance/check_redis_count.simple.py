import os
import redis
import time

url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
print(f"Connecting to {url}")
try:
    r = redis.from_url(url, socket_timeout=5)
    start = time.time()
    count = r.scard("rag:docs")
    print(f"rag:docs count: {count} (took {time.time()-start:.2f}s)")
    if count > 0:
        p = r.pipeline()
        members = r.srandmember("rag:docs", 5)
        print(f"Sample members: {members}")
except Exception as e:
    print(f"Error: {e}")
