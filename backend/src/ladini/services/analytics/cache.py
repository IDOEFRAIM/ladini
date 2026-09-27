"""Short read-through cache for the analytics endpoints (Valkey/Redis).

The cache is never a source of truth and never a dependency: any failure (client missing, server
down, timeout, corrupt entry) silently falls back to computing the payload. TTL comes from
`ANALYTICS_CACHE_TTL_SECONDS` (default 180 s; 0 disables). Keys embed a version so a deploy can
invalidate everything by bumping `_VERSION`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("Ladini.Analytics.Cache")

_VERSION = "v1"
DEFAULT_TTL = 180
_client: Any = None
_client_failed = False


def cache_ttl(default: int = DEFAULT_TTL) -> int:
    try:
        return max(0, int(os.getenv("ANALYTICS_CACHE_TTL_SECONDS", str(default))))
    except ValueError:
        return default


def _get_client() -> Any:
    global _client, _client_failed
    if _client is not None or _client_failed:
        return _client
    try:
        import redis.asyncio as aioredis

        from ladini.core.settings import settings

        _client = aioredis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=0.5, socket_connect_timeout=0.5)
    except Exception:  # noqa: BLE001 - cache is optional
        _client_failed = True
        logger.warning("analytics cache disabled: redis client unavailable")
    return _client


def make_key(endpoint: str, params: dict[str, Any]) -> str:
    digest = hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:32]
    return f"ladini:analytics:{_VERSION}:{endpoint}:{digest}"


async def cached(endpoint: str, params: dict[str, Any], producer: Callable[[], Awaitable[dict[str, Any]]],
                 *, ttl: Optional[int] = None, client: Any = None) -> dict[str, Any]:
    ttl = cache_ttl() if ttl is None else ttl
    if ttl <= 0:
        return await producer()
    key = make_key(endpoint, params)
    redis_client = client or _get_client()
    if redis_client is not None:
        try:
            hit = await redis_client.get(key)
            if hit:
                payload = json.loads(hit)
                payload["cache"] = "HIT"
                return payload  # type: ignore[no-any-return]
        except Exception:  # noqa: BLE001
            logger.warning("analytics cache read failed", exc_info=False)
    payload = await producer()
    if redis_client is not None:
        try:
            await redis_client.set(key, json.dumps(payload, default=str), ex=ttl)
        except Exception:  # noqa: BLE001
            logger.warning("analytics cache write failed", exc_info=False)
    payload["cache"] = "MISS"
    return payload


__all__ = ["cached", "make_key", "cache_ttl"]
