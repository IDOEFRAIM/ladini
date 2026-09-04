"""Doubles partagés pour les tests du LLM Gateway (`tests/unit/llm_gateway/`).

Conftest SCOPÉ à ce sous-package — même pattern que `tests/chaos/conftest.py`
(un conftest local plutôt que de bloater le conftest racine avec des doubles
qui ne servent qu'ici). `_FakeRedis` est plus riche que les `_FakeRedis`
minimalistes déjà présents ailleurs (`test_search_results_cache.py`,
`test_pending_photo_target.py` — juste get/setex/delete) car le Health
Registry a besoin de `set(nx=..., ex=...)`, `getdel`, et surtout
`.transaction(func, *keys)` (WATCH/MULTI/EXEC) pour son écriture
lecture-modification-écriture atomique.

Le store sous-jacent (`dict`) est injectable — deux `_FakeRedis(store=même_dict)`
simulent deux processus (API + worker Celery) partageant le MÊME Redis, pour
prouver que l'état de santé est réellement PARTAGÉ (§11/Test 15 du brief).
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import pytest


class _FakePipeline:
    """Suffisant pour `HealthRegistry._update()` : `.get()` avant `.multi()`
    (immédiat), `.set()` après `.multi()` (appliqué directement — pas besoin
    d'une vraie sémantique de file d'attente pour un test mono-thread)."""

    def __init__(self, redis: "_FakeRedis"):
        self._redis = redis

    def get(self, key: str):
        return self._redis.get(key)

    def multi(self):
        return self

    def set(self, key: str, value: str, ex: Optional[int] = None, nx: bool = False):
        return self._redis.set(key, value, ex=ex, nx=nx)


class _FakeRedis:
    def __init__(self, store: Optional[Dict[str, Any]] = None):
        # (valeur, expire_at_epoch_ou_None)
        self._store: Dict[str, Any] = store if store is not None else {}

    def _expired(self, key: str) -> bool:
        entry = self._store.get(key)
        if entry is None:
            return False
        _, expire_at = entry
        return expire_at is not None and time.time() >= expire_at

    def get(self, key: str) -> Optional[str]:
        if self._expired(key):
            del self._store[key]
        entry = self._store.get(key)
        return entry[0] if entry else None

    def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: Optional[int] = None,
    ) -> bool:
        if self._expired(key):
            del self._store[key]
        if nx and key in self._store:
            return False
        expire_at = (time.time() + ex) if ex else None
        self._store[key] = (value, expire_at)
        return True

    def getdel(self, key: str) -> Optional[str]:
        if self._expired(key):
            del self._store[key]
        entry = self._store.pop(key, None)
        return entry[0] if entry else None

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self._store:
                del self._store[key]
                removed += 1
        return removed

    def transaction(self, func, *watches):
        pipe = _FakePipeline(self)
        func(pipe)


@pytest.fixture()
def fake_redis_store() -> Dict[str, Any]:
    """Le dict partagé — passer le MÊME à plusieurs `_FakeRedis(store=...)`
    pour simuler plusieurs processus sur le même Redis."""
    return {}


@pytest.fixture()
def fake_redis(fake_redis_store) -> _FakeRedis:
    return _FakeRedis(store=fake_redis_store)


def make_fake_redis(store: Optional[Dict[str, Any]] = None) -> _FakeRedis:
    return _FakeRedis(store=store)
