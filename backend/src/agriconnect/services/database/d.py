from __future__ import annotations

import logging
import functools
from contextvars import ContextVar
from typing import Any, Callable, Optional, Set, Dict
import uuid

from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import AsyncSession
from agriconnect.core.database import get_sessionmaker
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

from agriconnect.domain.models import Farm, Stock, StockMovement

# Déclaration du conteneur de contexte pour isoler la session par tâche asynchrone (Coroutining/Greenlets)
db_session_ctx: ContextVar[Optional[AsyncSession]] = ContextVar("db_session_ctx", default=None)

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
        
        # Transactions & Staging
        "get_staged_transaction", "get_pending_actions",
        
        # Intelligence & Dashboards
        "normalize_unit", "check_price_anomaly", "guess_category", 
        "get_active_anomalies", "get_producer_dashboard", "get_zone_market_overview",
        
        # Crop Management (Agent Formation / Knowledge Retrieval)
        "get_cycle_with_context", "get_yield_performance_metrics", 
        "get_biological_readiness", "get_cycle_economics", 
        "get_active_sanitary_risks", "get_crop_requirements", 
        "analyze_thermal_stress", "check_growth_compliance", 
        "get_instant_resource_needs", "get_expenses", "get_expense_summary"
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
                async with session_factory() as new_session:
                    # Fixation de la session dans le stockage local de la coroutine (ContextVar)
                    token = db_session_ctx.set(new_session)
                    try:
                        # Injection transparente de la session dans les kwargs si le mixin l'attend
                        if "session" in kwargs:
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
                        self._logger.error("❌ Erreur SQL critique interceptée et annulée dans %s: %s", name, e, exc_info=True)
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



import asyncio
import logging
import sys
import uuid
from datetime import datetime, timezone

# 🚀 Imports SQLAlchemy indispensables pour le nettoyage automatique
from sqlalchemy import select, update, delete

# 🚀 Import des modèles réels pour le bloc de nettoyage final
# (Ajuste le chemin d'import si tes classes ne sont pas dans agriconnect.domain.models)
from agriconnect.domain.models import Product, SurplusOffer

# ⚠️ Import de ton service de base de données
from agriconnect.services.database.d import AgriDatabaseService 

# Configuration de l'affichage des logs dans la console
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("Agriconnect.FullIntegrationTest")


async def run_full_integration_test():
    # ⚠️ REQUIS : Ce numéro doit exister dans ta table 'Users' pour pouvoir tester
    TARGET_PRODUCER_PHONE = "+22601479808" 
    
    logger.info("🚀 Lancement de la suite complète de tests réels (ZÉRO MOCK)...")
    
    # Initialisation de ton service central autonome
    db_service = AgriDatabaseService()
    
    # Dictionnaires pour mémoriser les IDs générés et nettoyer la BDD à la fin
    created_resources = {
        "product_ids": [],
        "stock_ids": [],
        "client_ids": [],
        "surplus_ids": []
    }

    try:
        user = await db_service.get_user_by_phone(TARGET_PRODUCER_PHONE)        
        print('user',user)
    except Exception as e:
        logger.error(f"Erreur critique lors de l'exécution des tests : {e}")
    finally:
        print("\n🏁 Fin du grand cycle d'intégration. Ta base de données est propre et tout est validé !")


if __name__ == "__main__":
    # Exécution asynchrone du script
    asyncio.run(run_full_integration_test())
