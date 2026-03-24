"""
Collector for FEWS NET Reports.
Integrates existing FewsNetScraper logic.
"""
from typing import List, Dict, Any
import logging
from datetime import datetime, timezone

from backend.ingestion.schema import RawDocument
from agriconnect.services.data_collection.documents.fews import FewsNetScraper

logger = logging.getLogger(__name__)

class FewsCollector:
    """Uses FewsNetScraper to fetch latest reports."""
    
    def __init__(self, country: str = "burkina-faso"):
        self.scraper = FewsNetScraper(country_slug=country)

    def run(self) -> List[Dict[str, Any]]:
        raw_docs = []
        try:
            # The original FewsNetScraper writes directly to console/disk or returns text?
            # It has methods like extract_deep_content(url) -> text.
            # But the 'run()' method prints to stdout.
            # We will reuse its helpers to implement a clean generator here.
            
            import requests
            from bs4 import BeautifulSoup
            
            # 1. Fetch listing
            logger.info(f"Fetching FEWS listing for {self.scraper.country_slug}")
            resp = requests.get(self.scraper.url, headers={'User-Agent': 'Mozilla/5.0'})
            if resp.status_code != 200:
                return []
                
            soup = BeautifulSoup(resp.text, 'html.parser')
            count = 0
            
            # Simple link scanning based on original logic
            for link in soup.find_all('a', href=True):
                href = link['href']
                # Filtering logic from original scraper
                if not (f"/{self.scraper.country_slug}/" in href and any(x in href for x in ["mise-jour", "perspectives", "key-message"])):
                    continue

                full_url = f"https://fews.net{href}" if href.startswith('/') else href
                title = link.get_text(strip=True)
                
                # Check idempotence or duplicate
                # In real world, we check against DB or S3 existence.
                # Here we proceed to fetch deeply.
                
                text_content = self.scraper.extract_deep_content(full_url)
                if text_content and len(text_content) > 500:
                    doc = RawDocument(
                        source_id=full_url,
                        source_type="fews_report",
                        url=full_url,
                        title=title,
                        content=text_content,
                        metadata={
                            "country": self.scraper.country_slug,
                            "fetched_at": datetime.now(timezone.utc).isoformat()
                        }
                    )
                    raw_docs.append(doc.model_dump())
                    count += 1
                    if count >= 3: break # Limit
                    
        except Exception as e:
            logger.error(f"FEWS collection failed: {e}")
            
        return raw_docs
