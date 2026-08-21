from __future__ import annotations

import logging
import types
from typing import Any, Callable, Dict, Optional, Set

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agriconnect.core.database import get_sessionmaker
from agriconnect.services.database.auction import AuctionMixin

# Imports des Mixins
from agriconnect.services.database.auth import AuthMixin

# ContextVar unifié : partagé avec BaseService/@transactional.
# Une session ouverte ici est visible depuis OrderService/UserContextService/ProductService
# et vice-versa — un seul pool, une seule transaction, zéro deadlock inter-service.
from agriconnect.services.database.base_service import db_session_ctx, transactional
from agriconnect.services.database.buyer import BuyerMixin
from agriconnect.services.database.buyer_verification import BuyerVerificationMixin
from agriconnect.services.database.category import PublicProductMixin
from agriconnect.services.database.escrow import EscrowMixin
from agriconnect.services.database.marketplace import MarketplaceMixin
from agriconnect.services.database.moderation import ModerationMixin
from agriconnect.services.database.producer import ProducerMgmtMixin
from agriconnect.services.database.product import ProductMixin
from agriconnect.services.database.utils import UtilsMixin


class AgriDatabaseService(
    AuthMixin,
    UtilsMixin,
    MarketplaceMixin,
    PublicProductMixin,
    BuyerMixin,
    BuyerVerificationMixin,
    ProducerMgmtMixin,
    ProductMixin,
    AuctionMixin,
    ModerationMixin,
    EscrowMixin,
):
    _logger = logging.getLogger("AgriConnect.DatabaseService")

    # ==================================================================
    # SESSION — lecture via ContextVar (partagé avec @transactional)
    # ==================================================================
    @property
    def session(self) -> Optional[AsyncSession]:
        """Session active de la tâche courante, gérée par @transactional."""
        return db_session_ctx.get()

    # ==================================================================
    # CONFIGURATION DES MÉTHODES DE LECTURE (READ-ONLY)
    # ==================================================================
    _READ_ONLY_METHODS: Set[str] = {
        # Auth, Identity & Profiles
        "get_user_by_phone",
        "get_user_by_id",
        "get_user_context",
        "get_trust_score",
        "get_agent_memory",
        "get_clients",
        "get_producer_profile",
        "get_buyer_profile",
        "get_producer_farm",
        # Marketplace & Stock
        "get_farms",
        "get_stocks",
        "get_stock_movements",
        "list_products",
        "search_products",
        "get_orders",
        "get_producer_stocks",
        # Auctions / bids (reads)
        "get_auction_bids",
        "get_producer_auctions",
        "get_my_active_bids",
        # Buyer transactional reads
        "validate_stock_availability_atomic",
        "get_transaction_summary",
        # Marketplace utilities
        "guess_category",
        # Finance reads
        "get_expenses",
        "get_expense_summary",
        # Producer reads
        "get_producer_orders",
        "get_offer_reservations",
        # Moderation / anti-abuse reads
        "get_account_status",
        "get_prohibited_terms",
        # Escrow reads
        "list_producer_escrowed_orders",
    }

    # ==================================================================
    # DISPATCHER — proxy pur vers @transactional
    # ==================================================================
    _BYPASS_DISPATCH: Set[str] = {
        "ensure_performance_indexes",
        "DatabaseServiceError",
        "IntegrityError",
        "session",
    }
    # Cache CLASSE : méthode brute → wrapper @transactional (décoration faite une
    # seule fois par méthode, jamais par appel). Clé = "nom:is_write".
    _DISPATCH_CACHE: Dict[str, Callable] = {}

    def __getattribute__(self, name: str) -> Any:
        # Fast-path : attributs privés, dunders, bypass → accès direct.
        if name.startswith("_") or name in AgriDatabaseService._BYPASS_DISPATCH:
            return super().__getattribute__(name)

        attr = super().__getattribute__(name)
        if not callable(attr):
            return attr

        # Cache INSTANCE : méthode liée (bound method), construite une seule
        # fois par (instance, nom) via types.MethodType — l'idiome standard
        # pour lier une fonction à un objet sans définir de closure/lambda à
        # la volée à chaque accès d'attribut. Stocké dans le __dict__ natif de
        # l'instance (bypass explicite ci-dessus pour "_bound_dispatch").
        instance_dict = super().__getattribute__("__dict__")
        bound_cache = instance_dict.get("_bound_dispatch")
        if bound_cache is None:
            bound_cache = {}
            instance_dict["_bound_dispatch"] = bound_cache

        cached_bound = bound_cache.get(name)
        if cached_bound is not None:
            return cached_bound

        is_write = name not in AgriDatabaseService._READ_ONLY_METHODS
        cache_key = f"{name}:{is_write}"
        cache = AgriDatabaseService._DISPATCH_CACHE

        wrapped = cache.get(cache_key)
        if wrapped is None:
            # Récupère la méthode BRUTE (non liée) depuis le MRO pour que
            # @transactional reçoive une fonction ordinaire (self, *args, **kwargs).
            raw_fn = None
            for cls in type(self).__mro__:
                if name in cls.__dict__:
                    raw_fn = cls.__dict__[name]
                    break
            if raw_fn is None or not callable(raw_fn):
                return attr
            wrapped = transactional(write=is_write)(raw_fn)
            cache[cache_key] = wrapped

        bound = types.MethodType(wrapped, self)
        bound_cache[name] = bound
        return bound

    # ==================================================================
    # MÉTHODES SPÉCIFIQUES & EXCEPTIONS
    # ==================================================================

    async def ensure_performance_indexes(self, session: Optional[AsyncSession] = None):
        """Méthode de maintenance des index SQL exécutée de manière isolée."""
        from agriconnect.services.database.common import (
            PERFORMANCE_INDEX_DDL,
            SCHEMA_COLUMN_DDL,
        )

        async def _logic(sess: AsyncSession):
            for ddl in (*PERFORMANCE_INDEX_DDL, *SCHEMA_COLUMN_DDL):
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

    class DatabaseServiceError(Exception):
        pass

    class IntegrityError(DatabaseServiceError):
        pass



import asyncio
import os
import sys

# Assurez-vous que le chemin vers src est dans le sys.path si nécessaire
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "backend", "src")))


async def test_initiate_escrow():
    # 1. Initialiser la base de données (charge DATABASE_URL et configure le sessionmaker)
    print("Initialisation de la base de données...")

    # 2. Données de test (à adapter avec un vrai numéro et un vrai UUID de précommande DRAFT en base)
    test_buyer_phone = "+22601479800"
    test_preorder_id = "dff0a2dc-14be-4c09-8ba1-0c8e408dd124"
    test_lat = 12.3714 # Latitude Burkina Faso (ex: Ouagadougou)
    test_lon = -1.5197 # Longitude Burkina Faso

    print(f"Tentative d'initiation d'escrow pour la précommande {test_preorder_id}...")
    service = AgriDatabaseService()

    try:
        result = await service.initiate_escrow_payment(
                buyer_phone=test_buyer_phone,
                preorder_id=test_preorder_id,
                delivery_lat=test_lat,
                delivery_lon=test_lon
            )
        print("\n Succès ! Résultat de l'API Paydunya :")
        for k, v in result.items():
            print(f"  - {k}: {v}")
    except Exception as e:        
        print(f"\n Échec capturé lors du test : {type(e).__name__} - {e}")    
        


if __name__ == "__main__":
    # Exécution de la coroutine asynchrone
    asyncio.run(test_initiate_escrow())