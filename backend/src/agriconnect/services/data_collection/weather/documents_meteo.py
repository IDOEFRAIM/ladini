from __future__ import annotations

"""Compatibility adapter for Meteo bulletin scraping.

All scraping logic lives in `agriconnect.services.scraper.scrapers`.
This module keeps the historical `DocumentScraper` interface used by
weather collection services while delegating to `MeteoBurkinaScraper`.
"""

from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from agriconnect.core.schemas import RawDocument
from agriconnect.services.scraper.scrapers import MeteoBurkinaScraper


class DocumentScraper:
    """Legacy facade kept for backwards compatibility (zero scraping logic)."""

    def __init__(
        self,
        headless: bool = True,
        index_path: str = "produits/bulletin-agrometeologique-decadaire",
        base_url: str = "https://meteoburkina.bf",
    ):
        _ = headless  # legacy arg kept for API compatibility
        self.base_url = base_url.rstrip("/")
        self.index_path = index_path.lstrip("/")
        self.index_url = urljoin(f"{self.base_url}/", self.index_path)

        self._scraper = MeteoBurkinaScraper(
            config={
                "base_url": self.base_url,
                "search_urls": [self.index_url],
            }
        )

    @staticmethod
    def _document_to_legacy_record(doc: RawDocument) -> Dict[str, Any]:
        markdown = (doc.content_markdown or "").strip()
        return {
            "url": doc.url,
            "type": "pdf",
            "title": (doc.title or "Bulletin"),
            "content": markdown,
            "char_count": len(markdown),
            "metadata": doc.metadata or {},
        }

    def scrape_bulletins(self) -> Dict[str, Any]:
        # Use the scraper engine directly to keep a detailed content payload.
        doc, log = self._scraper.engine.run(self.index_url)
        if doc is None:
            return {
                "status": "ERROR",
                "message": str(log.error_trace or "harvest_failed"),
                "results": [],
            }

        results: List[Dict[str, Any]] = [self._document_to_legacy_record(doc)]
        return {
            "status": "SUCCESS",
            "message": f"{len(results)} bulletin(s) scrape(s)",
            "results": results,
        }


__all__ = ["DocumentScraper"]
