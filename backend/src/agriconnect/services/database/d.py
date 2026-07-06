from __future__ import annotations

import logging
import functools
from contextvars import ContextVar
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
from agriconnect.services.database.transactions import TransactionsMixin
from agriconnect.services.database.intelligence import IntelligenceMixin
from agriconnect.services.database.dashboards import DashboardsMixin
from agriconnect.services.database.crop import CropMixin
from agriconnect.services.database.category import PublicProductMixin
from agriconnect.services.database.buyer import BuyerMixin
from agriconnect.services.database.buyer_verification import BuyerVerificationMixin
from agriconnect.services.database.producer import ProducerMgmtMixin
from agriconnect.services.database.product import ProductMixin
from agriconnect.services.database.auction import AuctionMixin

try:
    import asyncpg  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    asyncpg = None



# Déclaration du conteneur de contexte pour isoler la session par tâche asynchrone (Coroutining/Greenlets)
db_session_ctx: ContextVar[Optional[AsyncSession]] = ContextVar("db_session_ctx", default=None)


def _is_connection_lost(exc: Exception) -> bool:
    if asyncpg and isinstance(exc, asyncpg.exceptions.ConnectionDoesNotExistError):
        return True
    message = str(exc).lower()
    return "connection was closed" in message or "connection does not exist" in message

class AgriDatabaseService(
    AuthMixin, UtilsMixin,
    TransactionsMixin, IntelligenceMixin, DashboardsMixin, CropMixin,
    PublicProductMixin, BuyerMixin, BuyerVerificationMixin, ProducerMgmtMixin,
    ProductMixin, AuctionMixin
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
        # Auth, Identity & Profiles (Base/Auth)
        "get_user_by_phone", "get_user_by_id", "get_user_context", "get_trust_score",
        "get_agent_memory", "get_clients", "get_producer_profile", "get_buyer_profile",
        "get_producer_farm",
        
        # Marketplace & Stock
        "get_farms", "get_stocks", "get_stock_movements", "list_products", 
        "search_products", "get_orders", "list_market_matches",
        "get_producer_stocks",

        # Auctions / bids (reads)
        "get_auction_bids",

        # Buyer transactional reads (Grade Entreprise)
        "validate_stock_availability_atomic", "get_transaction_summary",
        
        # Transactions & Staging
        "get_staged_transaction", "get_pending_actions",
        
        # Intelligence & Dashboards
        "normalize_unit", "check_price_anomaly", "guess_category", 
        "get_active_anomalies", "get_producer_dashboard", "get_zone_market_overview",
        
        # Crop Management (Agent Formation / Knowledge Retrieval)
        "get_crop_profile",
        "get_cycle_with_context", "get_yield_performance_metrics", 
        "get_biological_readiness", "get_cycle_economics", 
        "get_active_sanitary_risks", "get_crop_requirements", 
        "analyze_thermal_stress", "check_growth_compliance", 
        "get_instant_resource_needs", "calculate_irrigation_need",
        "calculate_custom_fertilization", "evaluate_disease_and_climate_risk",
        "calculate_sowing_density", "check_soil_salinity_hazard",
        "get_expenses", "get_expense_summary"
    }

# ==================================================================
    # LE DISPATCHER AUTOMATIQUE (Version ContextVar - Alignée et Sécurisée)
    # ==================================================================
    def __getattribute__(self, name: str) -> Any:
        # 1. Récupération de l'attribut réel via la classe parente (méthode non liée ou propriété)
        attr = super().__getattribute__(name)

        # 2. Interception exclusive des méthodes publiques exécutables
        if (
            callable(attr) 
            and not name.startswith("_") 
            and name not in ["ensure_performance_indexes", "DatabaseServiceError", "IntegrityError"]
        ):
            @functools.wraps(attr)
            async def auto_transaction_wrapper(*args, **kwargs):
                # Détermination du mode transactionnel (Lecture seule vs Écriture)
                is_read_only = name in self._READ_ONLY_METHODS
                should_commit = not is_read_only

                # 👀 CAS A : Une session existe déjà dans le contexte de la tâche (Transaction Imbriquée)
                existing_session = db_session_ctx.get()
                if existing_session is not None:
                    try:
                        # Injection transparente de la session si le mixin l'attend
                        import inspect
                        sig = inspect.signature(attr)
                        if "session" in sig.parameters and "session" not in kwargs:
                            kwargs["session"] = existing_session
                            
                        # On réutilise la session du contexte actuel sans altérer le self décalé
                        return await attr(*args, **kwargs)
                    except TypeError as e:
                        self._logger.error("❌ Erreur de signature dans le wrapper (Session existante) pour %s: %s", name, e)
                        raise e
                    except Exception as e:
                        self._logger.error("❌ Erreur interceptée dans une transaction imbriquée pour %s: %s", name, e)
                        raise

                # 👀 CAS B : Sommet de la pile d'exécution -> Génération de la Session Racine (Étanche)
                session_factory = get_sessionmaker()
                if session_factory is None:
                    raise RuntimeError("Database sessionmaker unavailable; ensure init_db() ran")

                for attempt in range(2):
                    async with session_factory() as new_session:
                        # Fixation de la session dans le stockage local de la coroutine (ContextVar)
                        token = db_session_ctx.set(new_session)
                        try:
                            # Injection transparente de la session si le mixin l'attend
                            # On utilise inspect pour vérifier si 'session' est dans la signature
                            import inspect
                            sig = inspect.signature(attr)
                            if "session" in sig.parameters and "session" not in kwargs:
                                kwargs["session"] = new_session

                            # Exécution de la méthode du mixin
                            res = await attr(*args, **kwargs)

                            # Commit uniquement si la méthode n'est pas enregistrée en READ-ONLY
                            if should_commit:
                                await new_session.commit()
                            return res

                        except TypeError as e:
                            await new_session.rollback()
                            self._logger.error("❌ Erreur de signature Python dans le wrapper pour %s: %s", name, e)
                            raise e
                        except Exception as e:
                            await new_session.rollback()
                            if _is_connection_lost(e) and attempt == 0:
                                self._logger.warning(
                                    "🔁 Session DB perdue (%s). Réinitialisation du pool et nouvelle tentative.",
                                    e,
                                )
                                db_session_ctx.reset(token)
                                try:
                                    await close_db()
                                except Exception:
                                    self._logger.exception("Échec lors de la fermeture du pool après perte de connexion")
                                session_factory = get_sessionmaker()
                                if session_factory is None:
                                    raise RuntimeError("Database sessionmaker unavailable après réinitialisation") from e
                                continue

                            self._logger.error(
                                "❌ Erreur SQL critique interceptée et annulée dans %s: %s",
                                name,
                                e,
                                exc_info=True,
                            )
                            raise
                        finally:
                            # Libération étanche du slot mémoire pour les coroutines concurrentes
                            db_session_ctx.reset(token)

            return auto_transaction_wrapper

        return attr

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
                res = await se.get_producer_orders(phone='+212782901759',producer_id=PRODUCER_ID)
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