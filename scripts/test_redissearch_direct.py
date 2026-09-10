#!/usr/bin/env python3
import os
import sys
import json
from typing import Any

try:
    from agriconnect.rag.providers.redis_search_provider import RedisSearchProvider
except Exception as e:
    print(json.dumps({"error": "import_failed", "detail": str(e)}))
    sys.exit(2)


def _get_redis_url() -> str:
    for k in ("AGRICONNECT_REDIS_URL", "REDIS_URL", "REDIS_URLS"):
        v = os.getenv(k)
        if v:
            return v
    try:
        from agriconnect.core.settings import settings

        v = getattr(settings, "REDIS_URL", None)
        if v:
            return v
    except Exception:
        pass
    return ""


def main():
    redis_url = _get_redis_url()
    if not redis_url:
        print(json.dumps({"error": "no_redis_url_provided"}))
        sys.exit(3)

    query = os.getenv("TEST_QUERY", "prix du mais Ouagadougou")
    top_k = int(os.getenv("TEST_TOP_K", "10"))

    try:
        prov = RedisSearchProvider(
            url=redis_url,
            dim=int(os.getenv("AGRICONNECT_EMBEDDING_DIM", "384")),
            ensure_index=False,
            socket_timeout=2,
            health_check_interval=5,
            retry_on_timeout=False,
            decode_responses=True,
        )
    except Exception as e:
        print(json.dumps({"error": "provider_init_failed", "detail": str(e)}))
        sys.exit(4)

    try:
        health = prov.health()
    except Exception as e:
        health = {"ok": False, "error": str(e)}

    try:
        results = prov.lexical_search(query, top_k=top_k)
    except Exception as e:
        print(json.dumps({"error": "search_failed", "detail": str(e), "health": health}))
        sys.exit(5)

    out = []
    for n in results:
        out.append({
            "id": getattr(n, "id", None),
            "score": getattr(n, "score", None),
            "metadata": getattr(n, "metadata", None),
            "text_preview": (getattr(n, "text", "") or "")[:300],
        })

    print(json.dumps({"health": health, "query": query, "count": len(out), "results": out}, ensure_ascii=False))


if __name__ == "__main__":
    main()
