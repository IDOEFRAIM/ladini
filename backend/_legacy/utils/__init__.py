"""
Services Utilities Module

Utilitaires transverses pour les services backend.

Modules:
- cache: Cache management avec TTL et storage

Usage:
    from backend.services.utils import Cache, StorageManager
    
    cache = Cache(ttl=3600)
    cache.set("key", "value")
    value = cache.get("key")
"""

from .cache import *

__version__ = "1.0.0"

__all__ = [
    "Cache",
    "StorageManager"
]
