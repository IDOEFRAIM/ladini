"""
Core Module — Fondations transverses Ladini.

- settings  : Configuration centralisée (Pydantic Settings)
- database  : Connexion PostgreSQL (SQLAlchemy)
- logger    : Logging unifié (stdlib)
- security  : Authentification et sanitisation
"""

from .database import check_connection, close_db, get_db, init_db
from .llm import get_groq_sdk, get_llm
from .logger import get_logger, setup_logging
from .security import (
    generate_request_id,
    get_api_key,
    sanitize_user_input,
    validate_api_key,
)
from .settings import settings

__all__ = [
    "settings",
    "setup_logging",
    "get_logger",
    "init_db",
    "close_db",
    "get_db",
    "check_connection",
    "get_api_key",
    "validate_api_key",
    "generate_request_id",
    "sanitize_user_input",
    "get_llm",
    "get_groq_sdk",
]
