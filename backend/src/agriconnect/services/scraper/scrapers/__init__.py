"""Cloud-native scraper package: jobs, shared primitives, and lambdas."""

from .shared import BaseScraper, ScraperQueueMessage, SQSProvider
from .jobs import (
    DataPlatformScraper,
    FaoDoiResolver,
    GoogleWorkspaceScraper,
    InstitutionalPdfHarvester,
    NewsScraper,
    PdfDownloader,
    TechnicalResourcesExplorer,
)

__version__ = "3.0.0"

__all__ = [
    "BaseScraper",
    "ScraperQueueMessage",
    "SQSProvider",
    "GoogleWorkspaceScraper",
    "PdfDownloader",
    "FaoDoiResolver",
    "NewsScraper",
    "DataPlatformScraper",
    "TechnicalResourcesExplorer",
    "InstitutionalPdfHarvester",
]
