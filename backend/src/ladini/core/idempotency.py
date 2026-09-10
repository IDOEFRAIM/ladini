"""Idempotency — atomic "has this happened yet" claim, shared primitive.

Single source of truth for the SET-NX-EX pattern: a mutation that must
happen AT MOST ONCE under concurrent or retried execution (two Celery
workers racing on the same event, a duplicate webhook delivery, two
requests confirming the same draft version at once) claims a key before
acting. Redis is single-threaded server-side, so this is safe under REAL
concurrent access — not just apparent safety from an in-process lock.

`api/response_dispatch.py::claim_response_item` predates this module and
implements the identical pattern independently (for outbound message
items) — not yet consolidated onto this primitive to avoid disturbing its
existing, separately-tested Redis-client monkeypatching contract. New
call sites (see `domain/procurement_draft.py`) should use this one.
"""

from __future__ import annotations

import logging
from typing import Optional

try:
    import redis
except ImportError:  # pragma: no cover - redis is a hard runtime dependency in prod
    redis = None  # type: ignore[assignment]

from ladini.core.settings import settings

logger = logging.getLogger("ladini.core.idempotency")

_client: Optional["redis.Redis"] = None


def _get_client() -> Optional["redis.Redis"]:
    global _client
    if _client is None and redis is not None:
        try:
            _client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        except Exception:
            logger.warning("IDEMPOTENCY_REDIS_UNAVAILABLE — garde désactivée")
            return None
    return _client


def claim_once(key: Optional[str], *, ttl_seconds: int = 3600) -> bool:
    """True si CETTE clé est réclamée maintenant pour la 1ère fois — l'appelant
    a le droit (et le devoir) d'agir. False si déjà réclamée — l'appelant ne
    doit RIEN refaire de plus.

    Fail-open (True) si Redis est indisponible ou si `key` est vide : mieux
    vaut une exécution en double rarissime (dégradation gracieuse, le métier
    en aval reste responsable de sa propre idempotence si elle existe) que
    plus jamais aucune exécution."""
    if not key:
        return True
    client = _get_client()
    if client is None:
        return True
    try:
        return bool(client.set(key, "1", ex=ttl_seconds, nx=True))
    except Exception:
        logger.warning("IDEMPOTENCY_REDIS_ERROR — garde ignorée (fail-open)")
        return True


__all__ = ["claim_once"]
