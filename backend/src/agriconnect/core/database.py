"""
Database — Connexion centralisée PostgreSQL (SQLAlchemy Async).
Optimisé pour Digital Ocean Managed Databases et les serveurs MCP.
"""

import logging
import ssl
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Tuple

try:
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
except ImportError:
    # Fallback for SQLAlchemy < 2.0 (Airflow uses 1.4)
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker

    def async_sessionmaker(*args, **kwargs):
        kwargs.setdefault('class_', AsyncSession)
        return sessionmaker(*args, **kwargs)

from sqlalchemy import text

from agriconnect.core.settings import settings

logger = logging.getLogger(__name__)

# ---------- Engine Asynchrone ----------

_async_engine = None
_AsyncSessionLocal = None
# Compatibility aliases for synchronous tools that expect `_engine` / `_SessionLocal`
_engine = None
_SessionLocal = None


def init_db() -> None:
    """Initialise le moteur asynchrone en nettoyant l'URL DigitalOcean."""
    global _async_engine, _AsyncSessionLocal
    
    if not settings.DATABASE_URL:
        logger.warning("DATABASE_URL non configurée.")
        return

  
    clean_url = settings.DATABASE_URL.split("?")[0]
    
    # TRANSFORMATION : On force le driver asynchrone
    url = clean_url.replace("postgresql://", "postgresql+asyncpg://")
    
    # CONFIGURATION SSL : mode explicite piloté par settings
    ssl_mode = str(getattr(settings, "DB_SSL_MODE", "verify-full") or "verify-full").strip().lower()
    ca_path = getattr(settings, "DB_CA_PATH", None)
    ssl_context = None
    if ssl_mode == "disable":
        ssl_context = False
        logger.warning("DB SSL mode is DISABLE (local-only).")
    elif ssl_mode == "require":
        ssl_context = "require"
        logger.warning("DB SSL mode is REQUIRE without certificate validation.")
    elif ssl_mode == "verify-full":
        if ca_path:
            # resolve relative to BASE_DIR if needed
            try:
                from pathlib import Path
                p = Path(ca_path)
                if not p.is_absolute():
                    p = Path(settings.BASE_DIR) / p
                p = p.resolve()
                if not p.exists():
                    raise FileNotFoundError(f"DB CA file not found at {p}")

                # Start from system trust store, then add project-specific CA.
                ssl_context = ssl.create_default_context()
                ssl_context.load_verify_locations(cafile=str(p))
                ssl_context.check_hostname = True
                ssl_context.verify_mode = ssl.CERT_REQUIRED
                logger.info("Using DB CA bundle at %s for SSL verification", p)
            except Exception as e:
                logger.error("Failed to load DB CA file (%s): %s", ca_path, e)
                raise
        else:
            logger.error("No DB_CA_PATH configured for verify-full SSL mode.")
            raise RuntimeError("DB_CA_PATH not configured; set settings.DB_CA_PATH or switch DB_SSL_MODE=require for local diagnostics")
    else:
        raise RuntimeError(f"Unsupported DB_SSL_MODE: {ssl_mode}. Expected verify-full|require|disable")

    _async_engine = create_async_engine(
        url,
        connect_args={
            "ssl": ssl_context,
            "prepared_statement_cache_size": 0,
        },
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,
    )
    
    _AsyncSessionLocal = async_sessionmaker(
        bind=_async_engine, 
        class_=AsyncSession, 
        expire_on_commit=False
    )
    logger.info("✅ Database engine asynchrone initialized for DigitalOcean.")



async def close_db() -> None:
    """Ferme proprement le pool de connexions asynchrone."""
    global _async_engine
    if _async_engine:
        await _async_engine.dispose()
        logger.info("🔒 Database engine fermé.")


@asynccontextmanager
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Générateur de session asynchrone utilisable de deux façons :
    1. FastAPI : async def route(db: AsyncSession = Depends(get_db))
    2. MCP Servers : async with get_db() as session:
    """
    if _AsyncSessionLocal is None:
        raise RuntimeError("Database non initialisée. Appelez init_db() d'abord.")
    
    async with _AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error(f"Erreur transactionnelle : {e}")
            raise
        finally:
            await session.close()


async def check_connection() -> bool:
    """Vérifie que la base est accessible (Asynchrone)."""
    if _async_engine is None:
        return False
    try:
        async with _async_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.warning("DB health check failed: %s", e)
        return False


async def check_connection_detailed() -> Tuple[bool, str]:
    """Like check_connection() but returns an explicit reason for diagnostics."""
    if _async_engine is None:
        return False, "engine_not_initialized"
    try:
        async with _async_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True, "ok"
    except Exception as e:
        msg = str(e)
        logger.warning("DB health check (detailed) failed: %s", msg)
        return False, msg


async def check_connection_aggressive() -> Tuple[bool, str]:
    """Aggressive readiness check for production MCP startup.

    Validates:
    - connectivity (SELECT 1)
    - UUID generator availability (gen_random_uuid)
    - pgvector extension presence
    - write capability in a temp object
    """
    if _async_engine is None:
        return False, "engine_not_initialized"

    try:
        async with _async_engine.begin() as conn:
            await conn.execute(text("SELECT 1"))

            has_uuid = await conn.scalar(text("SELECT to_regproc('gen_random_uuid') IS NOT NULL"))
            if not bool(has_uuid):
                return False, "gen_random_uuid_unavailable"

            has_vector = await conn.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector')")
            )
            if not bool(has_vector):
                return False, "pgvector_unavailable"

            await conn.execute(
                text("CREATE TEMP TABLE IF NOT EXISTS _agri_health_probe(v INT) ON COMMIT DROP")
            )
            await conn.execute(text("INSERT INTO _agri_health_probe(v) VALUES (1)"))
        return True, "ok"
    except Exception as e:
        msg = str(e)
        logger.warning("DB aggressive health check failed: %s", msg)
        return False, msg
    


if __name__ == "__main__":
    import asyncio

    async def test_database_lifecycle():
        print("\n🔍 --- DÉBUT DES TESTS DATABASE (Digital Ocean Ready) ---")
        
        # 1. Test Initialisation
        print("\n1️⃣ Initialisation du moteur...")
        init_db()
        
        # 2. Test de connectivité (Health Check)
        print("2️⃣ Vérification de la connectivité (Health Check)...")
        is_alive = await check_connection()
        if is_alive:
            print("   ✅ Connexion réussie à PostgreSQL !")
        else:
            print("   ❌ ÉCHEC de la connexion. Vérifiez DATABASE_URL et SSL.")
            return

        # 3. Test d'une transaction réelle via get_db
        print("3️⃣ Test d'une lecture/écriture via get_db...")
        try:
            async with get_db() as session:
                # On exécute une requête simple pour tester la session
                result = await session.execute(text("SELECT current_database(), now();"))
                db_name, current_time = result.fetchone()
                print(f"   ✅ Session active sur la base : '{db_name}'")
                print(f"   ✅ Heure du serveur : {current_time}")
        except Exception as e:
            print(f"   ❌ Erreur lors de l'utilisation de la session : {e}")

        # 4. Test de fermeture
        print("4️⃣ Fermeture du pool de connexions...")
        await close_db()
        
        # 5. Vérification après fermeture
        # Une fois fermé, le health check doit échouer ou être impossible
        is_alive_after = await check_connection()
        if not is_alive_after:
            print("   ✅ Moteur arrêté proprement.")
        
        print("\n🚀 --- TESTS TERMINÉS AVEC SUCCÈS ---")

    # Lancement du script de test
    try:
        asyncio.run(test_database_lifecycle())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"\n💥 Erreur fatale lors du test : {e}")