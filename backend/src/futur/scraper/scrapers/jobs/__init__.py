"""Scraper jobs (selectors and scraping logic)."""

from .google_workspace_scraper import GoogleWorkspaceScraper
from .pdf_downloader import PdfDownloader
from .fao_doi_resolver import FaoDoiResolver
from .news_scraper import NewsScraper
from .data_platform_scraper import DataPlatformScraper
from .technical_resources_explorer import TechnicalResourcesExplorer
from .institutional_pdf_harvester import InstitutionalPdfHarvester

__all__ = [
    "GoogleWorkspaceScraper",
    "PdfDownloader",
    "FaoDoiResolver",
    "NewsScraper",
    "DataPlatformScraper",
    "TechnicalResourcesExplorer",
    "InstitutionalPdfHarvester",
]
