"""
Logger centralisé pour Ladini.

Usage:
    from backend.core.logger import get_logger
    logger = get_logger(__name__)
"""

import logging
import sys
from typing import Optional

from ladini.core.settings import settings

_configured = False
_sentry_initialized = False


def init_sentry(level: int = logging.INFO) -> Optional[object]:
    """Initialise Sentry SDK si `SENTRY_DSN` est présent dans les settings.

    Idempotent et appelable indépendamment de `setup_logging()` — c'est ce
    que font `api/main.py` (lifespan FastAPI) et `api/tasks.py`
    (`worker_process_init` Celery) : `setup_logging()` complet appelle
    `logging.basicConfig(...)` + installe un `FileHandler`, ce qui
    reconfigurerait la racine du logging par-dessus celle déjà posée par
    gunicorn/uvicorn (ou par le process maître Celery) — risque de handlers
    dupliqués / sortie doublée. Seule l'init Sentry (jusqu'ici JAMAIS
    appelée nulle part dans le code — `SENTRY_DSN` était configuré en prod
    sans effet, bug corrigé ici) est nécessaire côté process API/worker.

    Returns the sentry client object or None if not initialized / not available.
    """
    global _sentry_initialized
    if _sentry_initialized:
        return None
    dsn = getattr(settings, "SENTRY_DSN", "")
    if not dsn:
        return None

    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration

        sentry_logging = LoggingIntegration(
            level=level,  # Capture info and above as breadcrumbs
            event_level=logging.ERROR,  # Send errors as events
        )

        sentry_sdk.init(
            dsn=dsn,
            integrations=[sentry_logging],
            environment=getattr(settings, "SENTRY_ENVIRONMENT", "production"),
            release=f"{settings.APP_NAME}@{settings.APP_VERSION}",
        )
        _sentry_initialized = True
        logging.getLogger("Ladini").info("Sentry initialized")
        return sentry_sdk
    except Exception:
        # If sentry not installed or fails, continue without raising
        logging.getLogger("Ladini").warning(
            "Sentry SDK not available or failed to init"
        )
        return None


# Alias rétrocompatible — ancien nom interne, conservé au cas où du code
# l'importait directement (aucun appelant trouvé dans le repo à ce jour).
_init_sentry_if_needed = init_sentry


def setup_logging(level: int = logging.INFO) -> None:
    """Configure le logging une seule fois, appelé au startup.

    Si `SENTRY_DSN` est configuré, initialise Sentry pour capturer erreurs.
    """
    global _configured
    if _configured:
        return

    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt=datefmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("app.log", encoding="utf-8"),
        ],
    )

    # Reduce noise from libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # Optionally initialize Sentry
    _init_sentry_if_needed(level=level)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Retourne un logger standard Python, nommé par module."""
    return logging.getLogger(name)


__all__ = ["setup_logging", "get_logger", "init_sentry"]
