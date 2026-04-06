"""backend.services.scraper

Lightweight package initializer with lazy imports to avoid circular
imports when the package is imported at application startup (eg. by
FastAPI/uvicorn). Import heavy submodules only when their symbols are
accessed.

Public API remains the same (names listed in ``__all__``), but the
actual imports happen lazily via ``__getattr__``.
"""

__all__ = [
    # Main Orchestrators
    'MasterHarvester',
    'lambda_handler',
    'ScraperOrchestrator',

    # Scraper framework
    'BaseScraper',
    'ScraperRegistry',
    'register_scraper',

    # Individual Scrapers
    'GoogleWorkspaceScraper',
    'PdfDownloader',
    'FaoDoiResolver',
    'NewsScraper',
    'DataPlatformScraper',
    'TechnicalResourcesExplorer',
    'InstitutionalPdfHarvester',
    'MeteoBurkinaScraper',
]


def __getattr__(name: str):
    """Lazy import symbols on first access to avoid circular imports."""
    if name in ('MasterHarvester', 'lambda_handler'):
        from .master_harvester import MasterHarvester, lambda_handler
        return MasterHarvester if name == 'MasterHarvester' else lambda_handler

    if name == 'ScraperOrchestrator':
        from .scraper_orchestrator import ScraperOrchestrator
        return ScraperOrchestrator

    if name in ('BaseScraper', 'ScraperRegistry', 'register_scraper'):
        from .scrapers import BaseScraper, ScraperRegistry, register_scraper
        return locals()[name]

    if name in ('GoogleWorkspaceScraper', 'PdfDownloader', 'FaoDoiResolver',
                'NewsScraper', 'DataPlatformScraper', 'TechnicalResourcesExplorer',
                'InstitutionalPdfHarvester',
                'MeteoBurkinaScraper'):
        from .scrapers import (
            GoogleWorkspaceScraper,
            PdfDownloader,
            FaoDoiResolver,
            NewsScraper,
            DataPlatformScraper,
            TechnicalResourcesExplorer,
            InstitutionalPdfHarvester,
            MeteoBurkinaScraper,
        )
        return locals()[name]

    # Backwards-compatible mappings for legacy names.
    if name == 'DocumentScraper':
        from agriconnect.services.data_collection.weather.documents_meteo import DocumentScraper
        return DocumentScraper

    if name == 'WeatherForecastService':
        from agriconnect.services.data_collection.weather.weather_forecast import WeatherForecastService
        return WeatherForecastService

    raise AttributeError(f"module {__name__} has no attribute {name}")


__version__ = "2.0.0"
__author__ = "Agribot-AI Team"
