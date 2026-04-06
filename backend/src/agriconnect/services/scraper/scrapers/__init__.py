"""Scraper bricks registry and implementations."""

from .base import BaseScraper
from .registry import ScraperRegistry, register_scraper

from .google_workspace_scraper import GoogleWorkspaceScraper
from .pdf_downloader import PdfDownloader
from .fao_doi_resolver import FaoDoiResolver
from .news_scraper import NewsScraper
from .data_platform_scraper import DataPlatformScraper
from .technical_resources_explorer import TechnicalResourcesExplorer
from .meteo_burkina import MeteoBurkinaScraper
from .institutional_pdf_harvester import InstitutionalPdfHarvester

__version__ = "2.0.0"

__all__ = [
    "BaseScraper",
    "ScraperRegistry",
    "register_scraper",
    "GoogleWorkspaceScraper",
    "PdfDownloader",
    "FaoDoiResolver",
    "NewsScraper",
    "DataPlatformScraper",
    "TechnicalResourcesExplorer",
    "MeteoBurkinaScraper",
    "InstitutionalPdfHarvester",
]
