"""
Collector for News Articles.
Wrap existing NewsScraper logic to produce standard RawDocument.
"""
from typing import List, Dict, Any
import json
import logging
from datetime import datetime, timezone

from backend.ingestion.schema import RawDocument
from agriconnect.services.scraper.scrapers.news_scraper import NewsScraper

logger = logging.getLogger(__name__)

class NewsCollector:
    """Wrapper for NewsScraper to integrate with Ingestion Pipeline."""

    def __init__(self):
        self.scraper = NewsScraper()

    def run(self) -> List[Dict[str, Any]]:
        """
        Runs the news scraper strategies and returns a list of RawDocument dicts.
        """
        raw_docs = []
        # Define target URLs to scrape
        target_urls = [
            "https://lefaso.net/spip.php?rubrique2", # Economie/Agriculture often here
            "https://www.sidwaya.info/rubrique/economie/",
        ]
        
        # Note: The original NewsScraper was designed to crawl/extract. 
        # We might need to adapt it to yield results instead of saving to disk directly,
        # or we read what it saved. 
        # For this refactoring, we will instantiate it and use its extraction logic directly if possible,
        # or mock the behavior if the original class only writes to disk.
        
        # Looking at NewsScraper, it has methods like extract_with_patterns.
        # But it lacks a public 'crawl' method that returns data in memory.
        # It seems we need to implement the crawling loop here using its helper methods.
        
        import requests
        from bs4 import BeautifulSoup
        
        for url in target_urls:
            try:
                # 1. Fetch listing page
                resp = requests.get(url, headers=self.scraper.headers, timeout=10)
                if resp.status_code != 200:
                    continue
                
                soup = BeautifulSoup(resp.text, "html.parser")
                
                # Simple link extraction (naive for now, can be improved)
                cw = 0
                for a in soup.select("a[href]"):
                    href = a["href"]
                    # Filter for likely article links
                    if "article" in href or "id_article" in href or ".html" in href:
                        full_url = requests.compat.urljoin(url, href)
                        
                        # 2. Extract Article
                        try:
                            art_resp = self.scraper.session.get(full_url, timeout=10)
                            art_soup = BeautifulSoup(art_resp.text, "html.parser")
                            
                            site_key = self.scraper.identify_site(full_url)
                            patterns = self.scraper.site_patterns.get(site_key, self.scraper.site_patterns['default'])
                            
                            content_elem = self.scraper.extract_with_patterns(art_soup, patterns['article'])
                            title_elem = self.scraper.extract_with_patterns(art_soup, patterns['title'])
                            
                            if content_elem and title_elem:
                                text = content_elem.get_text(separator="\n", strip=True)
                                title = title_elem.get_text(strip=True)
                                
                                if len(text) > 200: # Min content length
                                    doc = RawDocument(
                                        source_id=full_url,
                                        source_type="news_article",
                                        url=full_url,
                                        title=title,
                                        content=text,
                                        metadata={
                                            "site": site_key,
                                            "scraped_at": datetime.now(timezone.utc).isoformat()
                                        }
                                    )
                                    raw_docs.append(doc.model_dump())
                                    cw += 1
                                    if cw >= 5: break # Limit per source for demo
                        except Exception as e:
                            logger.warn(f"Failed to scrape article {full_url}: {e}")
                            
            except Exception as e:
                logger.error(f"Failed to crawl {url}: {e}")
                
        return raw_docs
