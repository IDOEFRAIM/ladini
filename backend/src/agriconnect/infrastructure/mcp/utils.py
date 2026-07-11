from __future__ import annotations

import asyncio
import atexit
import logging
from asyncio import Runner
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Coroutine, Optional, TypeVar

logger = logging.getLogger("MCP.Core.Utils")

_T = TypeVar("_T")

_LIFECYCLE_POOL: ThreadPoolExecutor | None = None


def _get_lifecycle_pool() -> ThreadPoolExecutor:
    global _LIFECYCLE_POOL
    if _LIFECYCLE_POOL is None:
        _LIFECYCLE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mcp-sync-bridge")
        atexit.register(_LIFECYCLE_POOL.shutdown, wait=False)
    return _LIFECYCLE_POOL


def _run_with_runner(coro: Coroutine[Any, Any, _T]) -> _T:
    with Runner() as runner:
        return runner.run(coro)


def run_coro_blocking(coro: Coroutine[Any, Any, _T], *, timeout: Optional[float] = None) -> _T:
    """Exécute une coroutine depuis du code SYNCHRONE, sans imbriquer d'event loop.

    ⚠️ DANGER D'AFFINITÉ DE LOOP (asyncpg) :
    Les connexions asyncpg sont liées à l'event loop qui les a créées. Si cette
    fonction est appelée DEPUIS un loop déjà actif, elle exécute la coroutine sur
    un NOUVEAU loop (dans un thread dédié) — toute connexion DB obtenue du pool
    partagé lèvera alors « Future attached to a different loop ».

    → RÈGLE : ne PAS router de travail DB via ce helper depuis un contexte async.
      Utilisez les méthodes `async` directement. Ce helper est réservé aux
      véritables appelants synchrones (pas de loop en cours), typiquement le
      démarrage/arrêt de serveur et les ponts sync legacy sans I/O DB.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run_with_runner(coro)

    logger.debug(
        "run_coro_blocking appelé depuis un event loop actif → offload thread "
        "(éviter tout I/O DB asyncpg par ce chemin)."
    )
    future = _get_lifecycle_pool().submit(_run_with_runner, coro)
    return future.result(timeout=timeout)
