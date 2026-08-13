"""Cible de la PROCHAINE photo envoyée par un producteur/acheteur.

Permet de lier automatiquement une photo à l'offre (Bid) ou à l'appel
d'offres (Auction) que l'utilisateur vient de créer/proposer, sans qu'il ait
besoin de préciser une commande — écrit depuis
``nodes/rendering/success.py`` (LangGraph, juste après un `place_bid`/
`create_auction` réussi) et lu depuis ``workers/media/product_photo_task.py``
(Celery, quand une photo arrive). Module volontairement SANS dépendance
``celery`` — même principe que ``services/search_results_cache.py``.
"""
from __future__ import annotations

import logging
from typing import Optional

import redis

from agriconnect.core.settings import settings

logger = logging.getLogger("agriconnect.services.pending_photo_target")

_TTL_SECONDS = 900  # 15 min — assez pour uploader une photo juste après l'action
_BID_PREFIX = "pending_bid_photo:"
_AUCTION_PREFIX = "pending_auction_photo:"

_redis_client: Optional["redis.Redis"] = None


def _redis() -> "redis.Redis":
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_client


def _mask(phone: str) -> str:
    return f"***{phone[-4:]}" if len(phone or "") >= 4 else "***"


def set_pending_bid_photo(phone: str, bid_id: str) -> None:
    if not phone or not bid_id:
        return
    try:
        _redis().setex(f"{_BID_PREFIX}{phone}", _TTL_SECONDS, str(bid_id))
    except Exception:
        logger.warning("PENDING_BID_PHOTO_WRITE_FAILED | phone=%s", _mask(phone))


def set_pending_auction_photo(phone: str, auction_id: str) -> None:
    if not phone or not auction_id:
        return
    try:
        _redis().setex(f"{_AUCTION_PREFIX}{phone}", _TTL_SECONDS, str(auction_id))
    except Exception:
        logger.warning("PENDING_AUCTION_PHOTO_WRITE_FAILED | phone=%s", _mask(phone))


def pop_pending_bid_photo(phone: str) -> Optional[str]:
    """Lit ET efface le marqueur (usage unique) — ``None`` si absent/expiré."""
    try:
        r = _redis()
        key = f"{_BID_PREFIX}{phone}"
        val = r.get(key)
        if val:
            r.delete(key)
        return val
    except Exception:
        return None


def pop_pending_auction_photo(phone: str) -> Optional[str]:
    try:
        r = _redis()
        key = f"{_AUCTION_PREFIX}{phone}"
        val = r.get(key)
        if val:
            r.delete(key)
        return val
    except Exception:
        return None
