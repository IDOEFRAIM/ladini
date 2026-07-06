"""
Core Module — Fondations transverses AgriConnect.

- settings  : Configuration centralisée (Pydantic Settings)
- database  : Connexion PostgreSQL (SQLAlchemy)
- logger    : Logging unifié (stdlib)
- security  : Authentification et sanitisation
"""

from .settings import settings
from .logger import setup_logging, get_logger
from .database import init_db, close_db, get_db, check_connection
from .security import get_api_key, validate_api_key, generate_request_id, sanitize_user_input
from .llm import get_llm, get_groq_sdk

__all__ = [
    "settings",
    "setup_logging", "get_logger",
    "init_db", "close_db", "get_db", "check_connection",
    "get_api_key", "validate_api_key", "generate_request_id", "sanitize_user_input",
    "get_llm", "get_groq_sdk",
]
