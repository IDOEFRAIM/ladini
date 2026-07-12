from __future__ import annotations

import asyncio
import logging
import functools
import inspect
from typing import Any, Callable, Optional, Set, Dict
import uuid

from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import AsyncSession
from agriconnect.core.database import get_sessionmaker, close_db
from datetime import datetime, timezone,timedelta
# Imports des Mixins
from agriconnect.services.database.auth import AuthMixin
from agriconnect.services.database.utils import UtilsMixin
from agriconnect.services.database.marketplace import MarketplaceMixin
from agriconnect.services.database.category import PublicProductMixin
from agriconnect.services.database.buyer import BuyerMixin
from agriconnect.services.database.buyer_verification import BuyerVerificationMixin
from agriconnect.services.database.producer import ProducerMgmtMixin
from agriconnect.services.database.product import ProductMixin
from agriconnect.services.database.auction import AuctionMixin
from agriconnect.services.database.moderation import ModerationMixin

# ContextVar unifié + helper rollback : partagés avec BaseService/@transactional.
# Une session ouverte ici est visible depuis OrderService/UserContextService/ProductService
# et vice-versa — un seul pool, une seule transaction, pas de deadlock.
from agriconnect.services.database.base_service import db_session_ctx, _safe_rollback

try:
    import asyncpg  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    asyncpg = None


def _is_connection_lost(exc: Exception) -> bool:
    if asyncpg and isinstance(exc, asyncpg.exceptions.ConnectionDoesNotExistError):
        return True
    message = str(exc).lower()
    return "connection was closed" in message or "connection does not exist" in message

class AgriDatabaseService(
    AuthMixin, UtilsMixin,
    MarketplaceMixin, PublicProductMixin, BuyerMixin, BuyerVerificationMixin,
    ProducerMgmtMixin, ProductMixin, AuctionMixin, ModerationMixin
):
    _logger = logging.getLogger("AgriConnect.DatabaseService")

    # ==================================================================
    # ACCÈS DYNAMIQUE À LA SESSION (Résout le problème du NoneType)
    # ==================================================================
    @property
    def session(self) -> Optional[AsyncSession]:
        """Lit dynamiquement la session stockée dans le contexte de la tâche en cours.
        
        Cette propriété permet à tous les Mixins d'accéder à `self.session` de manière 
        totalement transparente et unifiée.
        """
        return db_session_ctx.get()

    # ==================================================================
    # CONFIGURATION DES MÉTHODES DE LECTURE (READ-ONLY)
    # ==================================================================
    _READ_ONLY_METHODS: Set[str] = {
        # Auth, Identity & Profiles
        "get_user_by_phone", "get_user_by_id", "get_user_context", "get_trust_score",
        "get_agent_memory", "get_clients", "get_producer_profile", "get_buyer_profile",
        "get_producer_farm",

        # Marketplace & Stock
        "get_farms", "get_stocks", "get_stock_movements", "list_products",
        "search_products", "get_orders", "get_producer_stocks",

        # Auctions / bids (reads)
        "get_auction_bids",

        # Buyer transactional reads
        "validate_stock_availability_atomic", "get_transaction_summary",

        # Marketplace utilities
        "guess_category",

        # Finance reads
        "get_expenses", "get_expense_summary",

        # Producer reads
        "get_producer_orders",

        # Moderation / anti-abuse reads
        "get_account_status", "get_prohibited_terms",
    }

    # ==================================================================
    # DISPATCHER AUTOMATIQUE
    # ==================================================================
    # Méthodes pour lesquelles on N'intercepte PAS (accès direct).
    _BYPASS_DISPATCH: Set[str] = {
        "ensure_performance_indexes", "DatabaseServiceError", "IntegrityError",
        "session", "_READ_ONLY_METHODS", "_BYPASS_DISPATCH",
        "_SIG_CACHE", "_logger",
    }
    # Cache des signatures inspectées (évite inspect.signature() à chaque appel).
    _SIG_CACHE: Dict[str, bool] = {}

    @classmethod
    def _method_takes_session(cls, name: str, attr: Callable[..., Any]) -> bool:
        cached = cls._SIG_CACHE.get(name)
        if cached is not None:
            return cached
        try:
            takes = "session" in inspect.signature(attr).parameters
        except (TypeError, ValueError):
            takes = False
        cls._SIG_CACHE[name] = takes
        return takes

    def __getattribute__(self, name: str) -> Any:
        # Fast-path : attributs privés, dunders et bypass → accès direct sans wrap.
        if name.startswith("_") or name in AgriDatabaseService._BYPASS_DISPATCH:
            return super().__getattribute__(name)

        attr = super().__getattribute__(name)
        if not callable(attr):
            return attr

        method_name = name  # capture pour la closure
        method_takes_session = AgriDatabaseService._method_takes_session(method_name, attr)
        is_read_only = method_name in self._READ_ONLY_METHODS

        @functools.wraps(attr)
        async def auto_transaction_wrapper(*args, **kwargs):
            # CAS A — transaction imbriquée : on réutilise la session racine.
            existing_session = db_session_ctx.get()
            if existing_session is not None:
                if method_takes_session and "session" not in kwargs:
                    kwargs["session"] = existing_session
                return await attr(*args, **kwargs)

            # CAS B — sommet de pile : ouverture de session racine + retry sur perte connexion.
            session_factory = get_sessionmaker()
            if session_factory is None:
                raise RuntimeError("Database sessionmaker unavailable; ensure init_db() ran")

            for attempt in range(2):
                async with session_factory() as new_session:
                    token = db_session_ctx.set(new_session)
                    try:
                        if method_takes_session and "session" not in kwargs:
                            kwargs["session"] = new_session
                        res = await attr(*args, **kwargs)
                        if not is_read_only:
                            await new_session.commit()
                        return res
                    except asyncio.CancelledError:
                        # Timeout/annulation (ex: asyncio.wait_for côté MCP) : rollback
                        # protégé pour ne pas rendre une connexion avec tx ouverte.
                        await _safe_rollback(new_session)
                        raise
                    except Exception as exc:
                        await _safe_rollback(new_session)
                        # Retry UNE fois sur perte de connexion (failover DB).
                        if _is_connection_lost(exc) and attempt == 0:
                            self._logger.warning("Session DB perdue (%s). Reinit du pool.", exc)
                            try:
                                await close_db()  # concurrency-safe (snapshot-and-null)
                            except Exception:
                                self._logger.exception("Fermeture du pool en échec")
                            session_factory = get_sessionmaker()
                            if session_factory is None:
                                raise RuntimeError("Sessionmaker indisponible après reinit") from exc
                            continue
                        self._logger.error("Erreur SQL dans %s: %s", method_name, exc, exc_info=True)
                        raise
                    finally:
                        # Reset unique par itération (le token est recréé à chaque tour).
                        db_session_ctx.reset(token)

        return auto_transaction_wrapper

    # ==================================================================
    # MÉTHODES SPÉCIFIQUES & EXCEPTIONS
    # ==================================================================
    
    async def ensure_performance_indexes(self, session: Optional[AsyncSession] = None):
        """Méthode de maintenance des index SQL exécutée de manière isolée."""
        from agriconnect.services.database.common import PERFORMANCE_INDEX_DDL
        
        async def _logic(sess: AsyncSession):
            for ddl in PERFORMANCE_INDEX_DDL:
                try:
                    async with sess.begin_nested():
                        await sess.execute(text(ddl))
                except Exception: 
                    continue
            return {"status": "indexes_checked"}
        
        if session: 
            return await _logic(session)
        async with get_sessionmaker()() as s:
            res = await _logic(s)
            await s.commit()
            return res

    class DatabaseServiceError(Exception): pass
    class IntegrityError(DatabaseServiceError): pass
    

async def te():
    import time
    se = AgriDatabaseService()
    PRODUCER_ID = "0669b8b0-8e8b-4838-81de-aaef50538974"

    session_factory = get_sessionmaker()
    if session_factory is None:
        print("❌ Error retrieving market overview: sessionmaker unavailable (init_db non exécuté)")
        return

    async with session_factory() as session:
        token = db_session_ctx.set(session)
        try:
            try:
                start = time.time()
                res = await se.get_zone_by_name(name="Ouagadougou")
                end = time.time()
                print(f"Temps:{end-start}")
                print("✅ Market overview retrieved:", res)
            except Exception as e:
                print("❌ Error retrieving market overview:", e)
        finally:
            db_session_ctx.reset(token)

if __name__ == "__main__":
    import asyncio
    asyncio.run(te())