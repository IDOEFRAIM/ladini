from agriconnect.core.settings import settings
import redis
import os

url = settings.REDIS_URL or os.getenv("REDIS_URL")
print(f"Connecting to {url}")
r = redis.from_url(url)
count = r.scard("rag:docs")
print(f"rag:docs count: {count}")

docs = r.smembers("rag:docs")
if docs:
    first = list(docs)[0]
    print(f"Sample doc key: rag:doc:{first if isinstance(first, str) else first.decode()}")
    print(f"Sample doc type: {r.type(f'rag:doc:{first if isinstance(first, str) else first.decode()}')}") 
