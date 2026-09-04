"""
Database — Connexion centralisée PostgreSQL (SQLAlchemy Async).
Optimisé pour Digital Ocean Managed Databases et les serveurs MCP.
"""

import logging
import ssl
import threading
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Tuple

try:
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
except ImportError:
    # Fallback for SQLAlchemy < 2.0 (Airflow uses 1.4)
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    def async_sessionmaker(*args, **kwargs):
        kwargs.setdefault("class_", AsyncSession)
        return sessionmaker(*args, **kwargs)


import asyncio

from sqlalchemy import text

from agriconnect.core.settings import settings

logger = logging.getLogger(__name__)

# ---------- Engine Asynchrone ----------

_async_engine = None
_AsyncSessionLocal = None
# Compatibility aliases for synchronous tools that expect `_engine` / `_SessionLocal`
_engine = None
_SessionLocal = None

# Sérialise la création/destruction du moteur : sous un failover DB, plusieurs
# coroutines/threads peuvent tenter dispose+rebuild simultanément (thundering
# herd). Ce verrou garantit un seul moteur vivant à tout instant.
_engine_lock = threading.Lock()


def init_db() -> None:
    """Initialise le moteur asynchrone en nettoyant l'URL DigitalOcean."""
    global _async_engine, _AsyncSessionLocal
    # Idempotent + thread-safe : double-checked locking pour éviter deux engines.
    if _async_engine is not None:
        return
    with _engine_lock:
        if _async_engine is not None:
            return
        _init_db_locked()


def _init_db_locked() -> None:
    """Corps réel de l'initialisation, exécuté sous `_engine_lock`."""
    global _async_engine, _AsyncSessionLocal

    if not settings.DATABASE_URL:
        logger.warning("DATABASE_URL non configurée.")
        return
    # Normalize DATABASE_URL: remove surrounding quotes and whitespace
    db_url = str(settings.DATABASE_URL).strip()
    if db_url.startswith('"') and db_url.endswith('"'):
        db_url = db_url[1:-1].strip()
    if db_url.startswith("'") and db_url.endswith("'"):
        db_url = db_url[1:-1].strip()
    # Persist normalized value back to settings for downstream callers
    settings.DATABASE_URL = db_url

    # Ensure ORM models are imported so mappers/registers are configured
    try:
        # canonical domain models
        import agriconnect.domain.models as _domain_models  # noqa: F401
    except Exception:
        logger.debug(
            "Could not import agriconnect.domain.models at init time (will try fallback)"
        )

    clean_url = settings.DATABASE_URL.split("?")[0]

    # TRANSFORMATION : On force le driver asynchrone.
    # Deux schémas valides émis par les providers gérés selon le fournisseur :
    # DigitalOcean/Supabase/Neon → "postgresql://" ; Heroku Postgres → le
    # schéma court "postgres://" (RFC historique, toujours accepté par libpq
    # mais PAS par le registre de dialectes SQLAlchemy). Ne normaliser que
    # "postgresql://" laissait passer "postgres://" tel quel, provoquant
    # `NoSuchModuleError: Can't load plugin: sqlalchemy.dialects:postgres`
    # dès la bascule vers Heroku — corrigé en gérant explicitement les deux.
    if clean_url.startswith("postgresql+asyncpg://"):
        url = clean_url
    elif clean_url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + clean_url[len("postgresql://") :]
    elif clean_url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + clean_url[len("postgres://") :]
    else:
        url = clean_url

    # CONFIGURATION SSL : mode explicite piloté par settings
    ssl_mode = (
        str(getattr(settings, "DB_SSL_MODE", "verify-full") or "verify-full")
        .strip()
        .lower()
    )
    ca_path = getattr(settings, "DB_CA_PATH", None)
    ssl_context = None
    if ssl_mode == "disable":
        ssl_context = None
        logger.warning("DB SSL mode is DISABLE (local-only). No TLS will be requested.")
    elif ssl_mode == "require":
        # Create an SSL context that enables TLS but skips certificate
        # verification. This matches libpq's `sslmode=require` behaviour
        # (TLS but no CA verification) and is useful for managed DBs
        # where strict verification isn't configured for local diagnostics.
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
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
            raise RuntimeError(
                "DB_CA_PATH not configured; set settings.DB_CA_PATH or switch DB_SSL_MODE=require for local diagnostics"
            )
    else:
        raise RuntimeError(
            f"Unsupported DB_SSL_MODE: {ssl_mode}. Expected verify-full|require|disable"
        )

    # Disable asyncpg statement caches because DigitalOcean regularly rotates
    # schema metadata during maintenance windows, which invalidates prepared
    # plans and triggers InvalidCachedStatementError. Let SQLAlchemy re-prepare
    # on demand for each execution instead of relying on server caches.
    connect_args = {
        "prepared_statement_cache_size": 0,
        "statement_cache_size": 0,
    }
    # Only pass ssl when a context is present (asyncpg doesn't accept False)
    if ssl_context is not None:
        connect_args["ssl"] = ssl_context

    pool_size = getattr(settings, "DB_POOL_SIZE", 10) or 10
    max_overflow = getattr(settings, "DB_POOL_MAX_OVERFLOW", 5) or 5
    pool_timeout = getattr(settings, "DB_POOL_TIMEOUT", 30.0) or 30.0

    if pool_size < 5:
        logger.warning("DB_POOL_SIZE trop faible (%s) — forçage à 5.", pool_size)
        pool_size = 5
    if max_overflow < 0:
        logger.warning("DB_POOL_MAX_OVERFLOW négatif (%s) — forçage à 0.", max_overflow)
        max_overflow = 0
    if pool_timeout < 10:
        logger.warning(
            "DB_POOL_TIMEOUT trop faible (%s) — forçage à 10s.", pool_timeout
        )
        pool_timeout = 10.0

    _async_engine = create_async_engine(
        url,
        connect_args=connect_args,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        pool_pre_ping=True,
        echo=bool(getattr(settings, "DEBUG", False)),
    )

    _AsyncSessionLocal = async_sessionmaker(
        bind=_async_engine, class_=AsyncSession, expire_on_commit=False
    )
    logger.info("✅ Database engine asynchrone initialized for DigitalOcean.")


def get_engine():
    """Retourne l'engine asynchrone, en initialisant si nécessaire."""
    if _async_engine is None:
        init_db()
    return _async_engine


def get_sessionmaker():
    """Retourne le `async_sessionmaker` initialisé."""
    if _AsyncSessionLocal is None:
        init_db()
    return _AsyncSessionLocal


async def close_db() -> None:
    """Ferme proprement le pool. Sûr sous concurrence (snapshot-and-null).

    On récupère la référence au moteur ET on remet les globals à None de manière
    atomique (aucun await entre les deux) AVANT de disposer. Les appels
    concurrents voient alors `None` et n'essaient pas de disposer le même moteur
    deux fois — évite le thundering herd de dispose lors d'un failover DB.
    """
    global _async_engine, _AsyncSessionLocal
    engine_ref = _async_engine
    if engine_ref is None:
        return
    _async_engine = None
    _AsyncSessionLocal = None

    try:
        await engine_ref.dispose()
        logger.info("🔒 Database engine fermé.")
    except RuntimeError as e:
        # Happens when loop is already closed -> fallback to sync dispose
        if "Event loop is closed" in str(e):
            logger.warning(
                "Event loop closed during async dispose; attempting sync dispose."
            )
            try:
                sync = getattr(engine_ref, "sync_engine", None)
                if sync is not None:
                    sync.dispose()
                    logger.info("🔒 Sync engine disposed as fallback.")
            except Exception:
                logger.exception("Fallback sync dispose failed")
        else:
            logger.exception("Error disposing async engine: %s", e)


@asynccontextmanager
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Session context manager — caller must commit explicitly for writes.

    Usage::
        async with get_db() as session:
            await session.execute(...)
            await session.commit()   # required for writes
    """
    if _AsyncSessionLocal is None:
        init_db()
    if _AsyncSessionLocal is None:
        raise RuntimeError(
            "Database non initialisée. Configurez DATABASE_URL avant d'utiliser la DB."
        )

    async with _AsyncSessionLocal() as session:
        try:
            yield session
        except Exception as e:
            await session.rollback()
            logger.error(f"Erreur transactionnelle : {e}")
            raise


async def check_connection() -> bool:
    """Vérifie que la base est accessible (Asynchrone)."""
    if _async_engine is None:
        return False
    # small retry loop to handle transient network blips (e.g., WinError 64)
    last_exc = None
    for attempt in range(3):
        try:
            async with _async_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as e:
            last_exc = e
            logger.warning("DB health check failed (attempt %s/3): %s", attempt + 1, e)
            # exponential backoff
            await asyncio.sleep(0.2 * (2**attempt))
    logger.warning("DB health check final failure: %s", last_exc)
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

            has_uuid = await conn.scalar(
                text("SELECT to_regproc('gen_random_uuid') IS NOT NULL")
            )
            if not bool(has_uuid):
                return False, "gen_random_uuid_unavailable"

            has_vector = await conn.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector')"
                )
            )
            if not bool(has_vector):
                return False, "pgvector_unavailable"

            await conn.execute(
                text(
                    "CREATE TEMP TABLE IF NOT EXISTS _agri_health_probe(v INT) ON COMMIT DROP"
                )
            )
            await conn.execute(text("INSERT INTO _agri_health_probe(v) VALUES (1)"))
        return True, "ok"
    except Exception as e:
        msg = str(e)
        logger.warning("DB aggressive health check failed: %s", msg)
        return False, msg


async def ensure_extensions() -> dict:
    """Ensure required Postgres extensions are available.

    Creates `pg_trgm`, `vector` and attempts to enable a UUID generator
    provider (pgcrypto and/or uuid-ossp). This is idempotent.
    """
    if _async_engine is None:
        init_db()
    results = {"pg_trgm": None, "vector": None, "pgcrypto": None, "uuid-ossp": None}
    try:
        async with _async_engine.begin() as conn:
            # pg_trgm
            try:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
                results["pg_trgm"] = "ok"
            except Exception as e:
                results["pg_trgm"] = str(e)

            # vector (pgvector)
            try:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                results["vector"] = "ok"
            except Exception as e:
                results["vector"] = str(e)

            # pgcrypto (provides gen_random_uuid)
            try:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
                results["pgcrypto"] = "ok"
            except Exception as e:
                results["pgcrypto"] = str(e)

            # uuid-ossp (fallback for uuid_generate_v4)
            try:
                await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
                results["uuid-ossp"] = "ok"
            except Exception as e:
                results["uuid-ossp"] = str(e)
    except Exception as e:
        logger.warning("ensure_extensions failed: %s", e)
    return results


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
                result = await session.execute(
                    text("SELECT current_database(), now();")
                )
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
