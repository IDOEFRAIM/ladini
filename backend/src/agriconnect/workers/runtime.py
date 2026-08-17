"""Exécution asynchrone des crons sur la boucle persistante du worker.

Le moteur SQLAlchemy async est créé paresseusement sur la boucle unique du
worker Celery (voir ``api/tasks.py``). Réutiliser cette même boucle évite les
erreurs « attached to a different loop ». En dehors d'un worker (script, test),
on retombe sur ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, TypeVar

from agriconnect.core.database import get_sessionmaker
from agriconnect.services.database.base_service import db_session_ctx

logger = logging.getLogger("AgriConnect.Workers.Runtime")

T = TypeVar("T")


def run_async(coro: "Awaitable[T]") -> "T":
    """Exécute une coroutine sur la boucle persistante du worker si disponible."""
    loop = None
    try:
        from agriconnect.api import tasks as _tasks  # import tardif : évite un cycle

        loop = getattr(_tasks, "_loop", None)
    except Exception:  # pragma: no cover - défensif
        loop = None

    if loop is not None and not loop.is_closed():
        return loop.run_until_complete(coro)
    return asyncio.run(coro)


@asynccontextmanager
async def worker_session() -> AsyncIterator[Any]:
    """Ouvre une session DB hors requête HTTP, publiée dans le ContextVar partagé.

    Commit si le bloc réussit, rollback sinon.
    """
    session_factory = get_sessionmaker()
    if session_factory is None:
        raise RuntimeError(
            "Sessionmaker indisponible (init_db non exécuté / DATABASE_URL manquante)."
        )

    async with session_factory() as session:
        token = db_session_ctx.set(session)
        try:
            yield session
            await session.commit()
        except Exception:
            try:
                await session.rollback()
            except Exception:  # pragma: no cover
                logger.exception("Rollback de session worker en échec")
            raise
        finally:
            db_session_ctx.reset(token)
