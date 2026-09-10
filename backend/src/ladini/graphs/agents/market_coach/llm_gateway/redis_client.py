"""Factory Redis partagée pour le LLM Gateway.

L'audit (2026-09-02) a trouvé le pattern `_redis_client` dupliqué 4 fois dans
ce repo (search_results_cache.py, pending_photo_target.py, product_photo_task.py,
webhooks) — chacun son singleton lazy local, sans factory commune. Cette
fonction évite d'ajouter un 5e doublon ; elle ne migre PAS les call sites
existants (hors périmètre de cette PR).

Client SYNCHRONE (`redis.Redis`, pas `redis.asyncio`) — c'est déjà le seul
client utilisé dans tout ce repo (`redis = "^5.0"`, aucun usage de
`redis.asyncio` trouvé à l'audit). Les opérations Redis (GET/SET/EVAL) sont de
l'ordre de la milliseconde sur un Redis local/managé : appelées directement
depuis les nodes async du graphe sans `asyncio.to_thread`, exactement comme le
fait déjà `api/routes/whatsapp_webhook.py` pour son verrou d'idempotence.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("ladini.llm_gateway.redis")

_client: Optional[Any] = None


def get_redis(force_refresh: bool = False) -> Any:
    """Retourne (et met en cache) le client Redis partagé du LLM Gateway."""
    global _client
    if not force_refresh and _client is not None:
        return _client

    import redis

    from ladini.core.settings import settings

    _client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    logger.info("[llm_gateway] client Redis initialisé (url=%s)", _redact(settings.REDIS_URL))
    return _client


def _redact(url: str) -> str:
    """Masque tout credential éventuel dans l'URL avant de logger (règle
    §6/§52 du brief : jamais de secret dans les logs)."""
    if "@" not in url:
        return url
    scheme_and_creds, _, rest = url.partition("@")
    scheme = scheme_and_creds.split("://", 1)[0] if "://" in scheme_and_creds else ""
    return f"{scheme}://***@{rest}" if scheme else f"***@{rest}"
