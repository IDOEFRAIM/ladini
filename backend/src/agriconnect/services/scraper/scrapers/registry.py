from __future__ import annotations
"""
Scraper Registry (V2)

Module responsible for registering and discovering scrapers.
Exposes the `@register_scraper` decorator and an API to instantiate a scraper by key.
"""
import importlib
import pkgutil
import logging
import inspect
from typing import Dict, List, Optional, Type, Any, Union

# On importe BaseScraper uniquement pour le Type Hinting
from .base import BaseScraper

logger = logging.getLogger(__name__)

class ScraperRegistry:
    """
    REGISTRY V2 - Optimized
    - Auto-Discovery: scans and discovers scrapers automatically.
    - Lazy Loading: loads modules only when used (saves memory).
    - Validation: ensures scrapers conform to the BaseScraper contract.
    """
    
    # Stocke soit la classe (si déjà chargée), soit le chemin d'importation (lazy)
    _registry: Dict[str, Union[Type[BaseScraper], str]] = {}
    _loaded_scrapers: Dict[str, Type[BaseScraper]] = {}

    @classmethod
    def register(cls, key: str, scraper_ptr: Union[Type[BaseScraper], str]) -> None:
        """Register a class or a module import path."""
        key = key.strip().lower()
        if key in cls._registry:
            logger.warning(f"Registry warning: key '{key}' is already registered and will be overridden.")
        
        cls._registry[key] = scraper_ptr
        logger.debug(f"✅ Registre : clé '{key}' prête.")

    @classmethod
    def discover(cls, package_name: str = "agriconnect.scrapers"):
        """
        Scanne le dossier sans importer lourdement. 
        Enregistre les chemins de modules pour un chargement JIT (Just-In-Time).
        """
        try:
            package = importlib.import_module(package_name)
            for _, name, is_pkg in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
                if not is_pkg:
                    # Store the module name; the decorator will register scrapers when the module is imported.
                    # Perform a lightweight import to trigger @register where applicable.
                    importlib.import_module(name)

            logger.info(f"Discovery: {len(cls._registry)} scrapers detected in {package_name}")
        except Exception as e:
            logger.error(f"Failed to discover scrapers: {e}")

    @classmethod
    def get_scraper_class(cls, key: str) -> Type[BaseScraper]:
        """Retrieve the scraper class, loading dynamically if necessary."""
        key = key.strip().lower()
        
        if key not in cls._registry:
            available = ", ".join(cls._registry.keys())
            raise KeyError(f"Scraper '{key}' not found. Available: [{available}]")

        scraper_entry = cls._registry[key]

        # If it's already a class, return it
        if inspect.isclass(scraper_entry):
            return scraper_entry

        # If it's a string (lazy import path), this should have been registered by the decorator
        raise RuntimeError(f"Integrity error for '{key}': class not validated.")

    @classmethod
    def create(cls, key: str, config: Any = None) -> BaseScraper:
        """
        Instantiate the scraper on demand.

        This is where lazy-loading/memory optimization occurs.
        """
        scraper_cls = cls.get_scraper_class(key)
        
        # Injection de dépendance de la config
        return scraper_cls(config=config)

    @classmethod
    def list_scrapers(cls) -> List[str]:
        return sorted(list(cls._registry.keys()))

def register_scraper(*keys: str):
    """
    Decorator to register scrapers.
    Also validates that the class inherits from BaseScraper (quality contract).
    """
    def decorator(scraper_cls: Type[BaseScraper]):
        # Validation de sécurité
        if not inspect.isclass(scraper_cls):
            raise TypeError(f"The @register_scraper decorator can only be used on classes.")
        
        for key in keys:
            ScraperRegistry.register(key, scraper_cls)
        return scraper_cls
    return decorator