"""Cache Redis court-terme des résultats de recherche produit (acheteur).

Permet à la commande WhatsApp « photos <numéro> » de retrouver quel produit
un numéro affiché dans une liste de recherche désignait — sans coupler le
rendu LangGraph (nodes/rendering/success.py) au pipeline Celery : ce module
n'importe volontairement PAS `celery` (contrairement à
`workers/media/product_photo_task.py`, qui l'importe au niveau module), pour
que le rendu d'un résultat de recherche reste indépendant de l'infrastructure
de traitement asynchrone des photos.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import redis

from ladini.core.settings import settings

logger = logging.getLogger("ladini.services.search_results_cache")

_KEY_PREFIX = "last_search_results:"
_TTL_SECONDS = 1800  # 30 min — une recherche reste "récente" le temps de décider

_redis_client: Optional["redis.Redis"] = None


def _redis() -> "redis.Redis":
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_client


def key_for(phone: str) -> str:
    return f"{_KEY_PREFIX}{phone}"


def store_results(phone: str, entries: Dict[str, Dict[str, Any]]) -> None:
    """``entries``: ``{"1": {"id": ..., "name": ..., "images": [...]}, "2": {...}}``
    — clé = numéro affiché à l'acheteur (1-indexé), tel que rendu dans le texte.
    Best-effort : une panne Redis ne doit jamais casser l'affichage des
    résultats de recherche, juste désactiver la consultation photo."""
    if not phone or not entries:
        return
    try:
        _redis().setex(key_for(phone), _TTL_SECONDS, json.dumps(entries))
    except Exception:
        logger.warning(
            "SEARCH_RESULTS_CACHE_WRITE_FAILED | phone=%s",
            phone[-4:] if len(phone) >= 4 else "?",
        )


def load_results(phone: str) -> Optional[Dict[str, Dict[str, Any]]]:
    try:
        raw = _redis().get(key_for(phone))
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None
