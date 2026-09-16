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


# =====================================================================
# VALUE CACHE — get-or-compute, distinct de `claim_once` (qui ne porte
# qu'un booléen "déjà fait ?"). Ajouté pour l'idempotence d'un RÉSULTAT
# coûteux (ex: sortie d'un appel LLM payant) qu'un retry Celery ne doit
# jamais recalculer, seulement relire — voir
# `interpreter/routing.py::_cached_llm_completion`, seul appelant à ce
# jour. Même client Redis, même politique fail-open (indisponibilité =
# cache miss silencieux, jamais une exception qui bloquerait l'appelant).
# =====================================================================


def get_cached(key: Optional[str]) -> Optional[str]:
    """Relit une valeur mise en cache par `set_cached` sous la même clé.
    `None` si absente, expirée, ou si Redis est indisponible (fail-open :
    l'appelant retombe alors sur son chemin normal, ex. rappeler le LLM)."""
    if not key:
        return None
    client = _get_client()
    if client is None:
        return None
    try:
        return client.get(key)
    except Exception:
        logger.warning("IDEMPOTENCY_REDIS_ERROR — lecture cache ignorée (fail-open)")
        return None


def set_cached(key: Optional[str], value: str, *, ttl_seconds: int = 3600) -> None:
    """Persiste `value` sous `key` — best-effort, ne lève jamais (une panne
    Redis ici doit dégrader vers "pas de cache", jamais faire échouer le
    tour qui vient de produire cette valeur)."""
    if not key:
        return
    client = _get_client()
    if client is None:
        return
    try:
        client.set(key, value, ex=ttl_seconds)
    except Exception:
        logger.warning("IDEMPOTENCY_REDIS_ERROR — écriture cache ignorée (fail-open)")


def increment(key: Optional[str], *, ttl_seconds: int = 3600) -> Optional[int]:
    """Compteur PARTAGÉ (multi-workers) — `INCR` atomique côté Redis, avec un
    `EXPIRE` posé UNIQUEMENT sur le premier incrément (évite de repousser le
    TTL indéfiniment à chaque appel, qui empêcherait toute purge naturelle
    d'un message resté actif anormalement longtemps). Retourne la valeur
    APRÈS incrément (1 pour le tout premier appel sous cette clé), ou `None`
    si `key` est vide ou Redis indisponible — fail-open : l'appelant ne doit
    JAMAIS bloquer un traitement métier faute de pouvoir compter, seulement
    perdre cette dimension d'observabilité pour ce tour. Voir
    `interpreter/routing.py` (compteur `llm_call_index` par `message_sid`,
    2026-09-12) — seul appelant à ce jour."""
    if not key:
        return None
    client = _get_client()
    if client is None:
        return None
    try:
        value = client.incr(key)
        if value == 1:
            client.expire(key, ttl_seconds)
        return int(value)
    except Exception:
        logger.warning("IDEMPOTENCY_REDIS_ERROR — compteur ignoré (fail-open)")
        return None


def release(key: Optional[str]) -> None:
    """Supprime explicitement une clé posée par `claim_once` — SEUL moyen de
    permettre à une erreur RETRYABLE de rendre la main : sans ce `release`
    dans le chemin d'échec de l'appelant (voir
    `api/tasks.py::process_agent_task`), une réclamation posée AVANT un
    traitement qui échoue ensuite resterait verrouillée jusqu'à expiration
    du TTL — un retry Celery légitime (backoff ~5s+) la trouverait encore
    posée et abandonnerait le message à tort, transformant une panne
    TEMPORAIRE en perte DÉFINITIVE. Best-effort, ne lève jamais — une
    panne Redis ici laisse simplement la clé vivre jusqu'à son TTL naturel
    (dégradation : un retry pourrait être ignoré à tort jusqu'à expiration,
    jamais un double-traitement)."""
    if not key:
        return
    client = _get_client()
    if client is None:
        return
    try:
        client.delete(key)
    except Exception:
        logger.warning("IDEMPOTENCY_REDIS_ERROR — libération de clé ignorée (fail-open)")


__all__ = ["claim_once", "get_cached", "set_cached", "release", "increment"]
