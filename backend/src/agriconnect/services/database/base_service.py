"""BaseService + @transactional — gestionnaire unique du cycle de vie transactionnel.

@transactional est le SEUL cerveau transactionnel du backend, et il ignore tout
du domaine métier : le commit/rollback est piloté À 100% par le flux
d'exécution Python (exception levée ou non), jamais par le contenu d'un dict
de retour. Un échec métier doit être signalé en levant `BusinessRuleException`
(voir errors.py) — jamais en renvoyant `{"status": "error", ...}`.

  - Ouvre la session si et seulement si c'est l'appelant racine (ContextVar vide).
  - Commit uniquement si aucune exception n'a été levée.
  - Rollback sur TOUTE exception (métier ou technique) — c'est le seul signal.
  - Retry unique sur perte de connexion DB (failover managé).
  - Barrière anti-fuite : encapsule les erreurs techniques dans SafeDatabaseError ;
    les erreurs métier (BusinessRuleException, ValueError, KeyError) traversent
    intactes.
  - Nettoie le ContextVar dans le finally, quelle que soit l'issue.

Usage :
    @transactional(write=True)   → commit à la sortie si succès, rollback si exception
    @transactional(write=False)  → lecture seule, pas de commit

La session est partagée par tâche via db_session_ctx : un appel imbriqué réutilise
la session racine sans commit (le parent gère l'issue transactionnelle).

Deux points d'entrée possibles :
  1. AgriDatabaseService.__getattribute__ (outils MCP / agents LangGraph)
     → applique @transactional dynamiquement sur la méthode brute du mixin.
  2. Services standalone (OrderService, ProductService, UserContextService)
     → déclarent @transactional explicitement sur chaque méthode.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from contextvars import ContextVar
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from agriconnect.core.database import close_db, get_sessionmaker

logger = logging.getLogger("AgriConnect.Service")

# ContextVar partagé avec d.py (AgriDatabaseService). Une session ouverte d'un
# côté est réutilisée de l'autre — un seul pool, une seule transaction, zéro
# session imbriquée, zéro deadlock inter-service.
db_session_ctx: ContextVar[Optional[AsyncSession]] = ContextVar(
    "db_session_ctx", default=None
)


def _is_connection_lost(exc: Exception) -> bool:
    try:
        import asyncpg  # type: ignore
        if isinstance(exc, asyncpg.exceptions.ConnectionDoesNotExistError):
            return True
    except ImportError:
        pass
    msg = str(exc).lower()
    return "connection was closed" in msg or "connection does not exist" in msg


def transactional(*, write: bool = False):
    """Décorateur transactionnel unifié — seul gestionnaire de session du backend.

    Détecte automatiquement si la méthode décore attend `session` en paramètre
    (services standalone style BaseService) ou lit `self.session` via le ContextVar
    (mixins style AgriDatabaseService). Dans les deux cas, la logique de cycle de
    vie (ouverture, commit, rollback, retry, barrière erreurs) est identique.
    """
    def decorator(fn):
        _takes_session = "session" in inspect.signature(fn).parameters

        @functools.wraps(fn)
        async def wrapper(self, *args, **kwargs):
            # Import différé : errors.py ne dépend pas de base_service.py, mais
            # l'import au niveau module créerait un cycle au chargement du package.
            from agriconnect.services.database.errors import (
                SafeDatabaseError,
                is_safe_business_exception,
                sanitize_error_message,
                scrub_error_result,
            )

            existing = db_session_ctx.get()
            if existing is not None:
                # ── Appel imbriqué ────────────────────────────────────────────
                # La session racine gère commit / rollback. On ne touche pas au
                # ContextVar (le token appartient à la racine).
                if _takes_session and "session" not in kwargs:
                    return await fn(self, existing, *args, **kwargs)
                return await fn(self, *args, **kwargs)

            # ── Appel racine ──────────────────────────────────────────────────
            session_factory = get_sessionmaker()
            if session_factory is None:
                raise RuntimeError(
                    "Database sessionmaker unavailable; ensure init_db() ran"
                )

            for attempt in range(2):
                async with session_factory() as session:
                    token = db_session_ctx.set(session)
                    try:
                        if _takes_session and "session" not in kwargs:
                            res = await fn(self, session, *args, **kwargs)
                        else:
                            res = await fn(self, *args, **kwargs)

                        # Le commit est piloté à 100% par le flux d'exécution :
                        # aucune exception levée == succès == commit. La couche
                        # transactionnelle n'inspecte JAMAIS le contenu du
                        # résultat (pas de parsing de `status`/`"error"`/etc.) —
                        # un échec métier DOIT être signalé par une exception
                        # (BusinessRuleException), jamais par un dict de retour.
                        if write:
                            await session.commit()

                        # Barrière anti-fuite finale : neutralise tout message
                        # technique qui aurait malgré tout fui dans un dict
                        # renvoyé sans lever d'exception (défense en profondeur,
                        # orthogonale au contrôle commit/rollback ci-dessus).
                        return scrub_error_result(res) if isinstance(res, dict) else res

                    except asyncio.CancelledError:
                        # Timeout / annulation (asyncio.wait_for côté MCP) :
                        # rollback obligatoire pour ne pas rendre une connexion
                        # avec une transaction ouverte au pool.
                        await _safe_rollback(session)
                        raise

                    except Exception as exc:
                        await _safe_rollback(session)

                        # Retry unique sur perte de connexion (failover DB managée).
                        if _is_connection_lost(exc) and attempt == 0:
                            logger.warning(
                                "Session DB perdue (%s). Reinit du pool.", exc
                            )
                            try:
                                await close_db()  # concurrency-safe snapshot-and-null
                            except Exception:
                                logger.exception("Fermeture du pool en échec")
                            session_factory = get_sessionmaker()
                            if session_factory is None:
                                raise RuntimeError(
                                    "Sessionmaker indisponible après reinit"
                                ) from exc
                            continue  # un seul retry, ouvre une nouvelle session

                        # ── Barrière anti-fuite — valve à sens unique ─────────
                        #
                        # Exception MÉTIER (ValueError, KeyError, stock insuffisant,
                        # prix nul, produit introuvable…) → re-raise intact. Type
                        # ET message traversent : l'agent doit savoir précisément
                        # pourquoi l'action a échoué pour guider l'utilisateur WhatsApp.
                        #
                        # Exception TECHNIQUE (IntegrityError, asyncpg, timeout,
                        # driver ORM…) → jamais divulguée telle quelle. Stack
                        # loggée côté serveur, SafeDatabaseError (message générique)
                        # levée à la place.
                        if is_safe_business_exception(exc):
                            logger.info(
                                "Exception métier dans %s (transmise intacte): %s",
                                fn.__name__,
                                exc,
                            )
                            raise

                        logger.error(
                            "Erreur technique dans %s: %s",
                            fn.__name__,
                            exc,
                            exc_info=True,
                        )
                        raise SafeDatabaseError(
                            sanitize_error_message(exc, context=fn.__name__)
                        ) from exc

                    finally:
                        # Reset systématique : même si continue/raise/return,
                        # le ContextVar est propre pour la prochaine tâche asyncio.
                        db_session_ctx.reset(token)

                break  # sortie normale si pas de `continue` (retry connexion)

        return wrapper

    return decorator


async def _safe_rollback(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:
        logger.debug(
            "Rollback échoué (session probablement déjà fermée)", exc_info=True
        )


class BaseService:
    """Socle commun pour les services standalone (@transactional explicite)."""

    _logger = logging.getLogger("AgriConnect.Service")

    @property
    def session(self) -> Optional[AsyncSession]:
        return db_session_ctx.get()
