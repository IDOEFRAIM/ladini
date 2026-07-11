"""BaseService + @transactional — remplace le dispatch magique (__getattribute__).

Chaque méthode de service déclare EXPLICITEMENT son intention transactionnelle :
    @transactional(write=True)   -> commit à la sortie
    @transactional(write=False)  -> lecture seule (rollback implicite en sortie)

La session est partagée par tâche via un ContextVar : une méthode appelée à
l'intérieur d'une autre réutilise la session racine (transaction imbriquée),
sinon une session racine est ouverte. Signature : `async def m(self, session, ...)`.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from contextvars import ContextVar
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

# Source unique de vérité pour le moteur/pool : core.database (le MÊME que le
# dispatcher AgriDatabaseService). Ne JAMAIS ouvrir un second engine ici, sinon
# on double le nombre de connexions vers la DB managée → épuisement du pool.
from agriconnect.core.database import get_sessionmaker

logger = logging.getLogger("AgriConnect.Service")

# ContextVar partagé avec d.py (dispatcher). Une session ouverte d'un côté est
# réutilisée de l'autre (transaction imbriquée) — un seul pool, une seule tx.
db_session_ctx: ContextVar[Optional[AsyncSession]] = ContextVar("db_session_ctx", default=None)


def transactional(*, write: bool = False):
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(self, *args, **kwargs):
            existing = db_session_ctx.get()
            if existing is not None:
                # Transaction imbriquée : réutilise la session racine, ne commit pas
                # (le commit/rollback appartient à la racine de la pile).
                return await fn(self, existing, *args, **kwargs)

            session_factory = get_sessionmaker()
            if session_factory is None:
                raise RuntimeError("Database sessionmaker unavailable; ensure init_db() ran")

            async with session_factory() as session:
                token = db_session_ctx.set(session)
                try:
                    result = await fn(self, session, *args, **kwargs)
                    if write:
                        await session.commit()
                    return result
                except asyncio.CancelledError:
                    # Timeout/annulation : rollback protégé avant de propager pour
                    # ne pas rendre au pool une connexion avec une tx ouverte.
                    await _safe_rollback(session)
                    raise
                except Exception:
                    await _safe_rollback(session)
                    logger.exception("Rollback transactionnel dans %s", getattr(fn, "__name__", "?"))
                    raise
                finally:
                    db_session_ctx.reset(token)

        return wrapper

    return decorator


async def _safe_rollback(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:
        logger.debug("Rollback échoué (session probablement déjà fermée)", exc_info=True)


class BaseService:
    """Socle commun : accès à la session courante (contexte de tâche)."""

    _logger = logging.getLogger("AgriConnect.Service")

    @property
    def session(self) -> Optional[AsyncSession]:
        return db_session_ctx.get()
