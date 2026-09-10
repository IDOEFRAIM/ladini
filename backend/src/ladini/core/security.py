"""
Security Module — Authentification et sécurité pour Ladini.

Fournit :
- Validation de clés API
- Génération de request ID
- Sanitization des entrées utilisateur
"""

import logging
import secrets
from typing import Optional

from ladini.core.settings import settings

logger = logging.getLogger(__name__)


def get_api_key() -> Optional[str]:
    """Récupère la clé API LLM configurée."""
    return settings.llm_api_key or None


def validate_api_key(key: str) -> bool:
    """Vérifie qu'une clé API est valide (non vide et format Groq)."""
    if not key or len(key) < 10:
        return False
    # Groq keys start with "gsk_"
    return key.startswith("gsk_")


def generate_request_id() -> str:
    """Génère un identifiant unique pour le suivi des requêtes."""
    return secrets.token_hex(16)


def sanitize_user_input(text: str, max_length: int = 2000) -> str:
    """
    Nettoie l'entrée utilisateur :
    - Limite la longueur
    - Supprime les caractères de contrôle
    """
    if not text:
        return ""
    # Tronquer
    text = text[:max_length]
    # Supprimer les caractères de contrôle (sauf newline, tab)
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or (ord(ch) >= 32))
    return text.strip()


__all__ = [
    "get_api_key",
    "validate_api_key",
    "generate_request_id",
    "sanitize_user_input",
]
