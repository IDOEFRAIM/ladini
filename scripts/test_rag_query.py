import os
import asyncio
import json

os.environ.setdefault('REDIS_URL', 'redis://localhost:6380')

from agriconnect.services.rag_service import RagService

async def main():
    svc = RagService()
    q = "Quels sont les conseils pour la fertilisation du maïs en zone tempérée?"
    res = await svc.search_documents(q, level="debutant", top_k=5)
    print(json.dumps(res, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    asyncio.run(main())
