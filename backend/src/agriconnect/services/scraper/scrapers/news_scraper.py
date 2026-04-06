from __future__ import annotations

"""
News scraper (V2)

This module implements `NewsScraper`, a "Zero-IO" scraper that:
- fetches an article page into memory;
- extracts structured content into Markdown (via `extract_markdown_from_html`);
- normalizes and returns a `RawDocument` and a `ScraperLog` without writing to disk.

Contract: `scrape(url) -> (RawDocument|None, meta_dict)` and `run(url)` wraps and returns `(RawDocument, ScraperLog)`.
"""

from typing import Dict, Optional, Tuple
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup

from agriconnect.core.schemas import RawDocument
from agriconnect.core.scraper_utils import extract_markdown_from_html

from .base import BaseScraper
from .registry import register_scraper


@register_scraper("news", "news_article", "article")
class NewsScraper(BaseScraper):
    """Zero-IO news scraper with markdown-preserving extraction."""

    name = "news_scraper"
    version = "2.0.0"

    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config=config)
        cfg = config or {}
        self.headers = dict(self.session.headers)
        self.site_patterns = {
            "lefaso.net": {"article": ["article", "main"], "title": ["h1"]},
            "sidwaya.info": {"article": ["article", "main"], "title": ["h1"]},
            "default": {"article": ["article", "main", "body"], "title": ["h1", "title"]},
        }
        if isinstance(cfg.get("site_patterns"), dict):
            for site_key, site_conf in cfg["site_patterns"].items():
                if not isinstance(site_conf, dict):
                    continue
                current = self.site_patterns.get(site_key, {"article": ["article", "main"], "title": ["h1", "title"]})
                article_selectors = site_conf.get("article")
                title_selectors = site_conf.get("title")
                if isinstance(article_selectors, list) and article_selectors:
                    current["article"] = article_selectors
                if isinstance(title_selectors, list) and title_selectors:
                    current["title"] = title_selectors
                self.site_patterns[site_key] = current
        # Keep native type (list or string) from YAML/config.
        self.preferred_selectors = cfg.get(
            "preferred_selectors", "article, main, #content, .post-content"
        )

    def identify_site(self, url: str) -> str:
        domain = urlparse(url).netloc.lower()
        for key in self.site_patterns:
            if key != "default" and key in domain:
                return key
        return "default"

    @staticmethod
    def extract_with_patterns(soup: BeautifulSoup, patterns):
        for pattern in patterns:
            elem = soup.select_one(pattern)
            if elem is not None:
                return elem
        return None

    def scrape(self, url: str) -> Tuple[Optional[RawDocument], Dict]:
        response = self._request(url)
        html = response.text
        soup = BeautifulSoup(html, "html.parser")

        # Use site-specific patterns to isolate the article node when possible
        site_id = self.identify_site(url)
        patterns = self.site_patterns.get(site_id, self.site_patterns["default"])
        article_node = self.extract_with_patterns(soup, patterns.get("article", []))

        # Title: prefer H1 inside article, then configured title selectors, then document title
        title_elem = None
        if article_node is not None:
            title_elem = self.extract_with_patterns(article_node, patterns.get("title", []))
        if not title_elem:
            title_elem = soup.select_one("title")
        title = (title_elem.get_text(strip=True) if title_elem and title_elem.get_text() else "Article")

        target_html = self._prepare_markdown_html(
            str(article_node) if article_node is not None else html,
            base_url=url,
            preferred_selectors=self.preferred_selectors,
        )
        markdown = extract_markdown_from_html(target_html, include_links=True)

        # Try to extract publication date (best-effort)
        # Pass both the fragment (article_node) and the full soup so meta tags
        # in <head> can be detected when the fragment lacks them.
        pub_date = self._normalize_publication_date(self._extract_pub_date(article_node, soup))
        language = self._extract_language_code(soup)
        quality = self._assess_extraction_quality(markdown, html)

        if quality.get("blocked_by_waf"):
            return None, {
                "http_status": response.status_code,
                "bytes_downloaded": len(response.content or b""),
                "raw_payload": html,
                "blocked_by_waf": True,
            }

        if not markdown.strip():
            return None, {
                "http_status": response.status_code,
                "bytes_downloaded": len(response.content or b""),
                "raw_payload": html,
            }

        doc = self._build_document(
            url=url,
            title=title,
            markdown=markdown,
            metadata={
                "source_type": "news_article",
                "source_domain": self._source_domain(url),
                "content_type": response.headers.get("Content-Type", ""),
                "publication_date": pub_date,
                "language": language,
                "extraction_method": "html_to_markdown",
                "partial_extraction": bool(quality.get("partial_extraction")),
            },
            language=language,
        )
        return doc, {
            "http_status": response.status_code,
            "bytes_downloaded": len(response.content or b""),
            "raw_payload": html,
            "partial_extraction": bool(quality.get("partial_extraction")),
        }

    def run_listing(self, listing_url: str, limit: int = 10):
        """Utility method returning in-memory docs from a listing page."""
        response = self._request(listing_url)
        soup = BeautifulSoup(response.text, "html.parser")
        docs = []
        seen = set()
        domain = urlparse(listing_url).netloc.lower()
        for anchor in soup.select("a[href]"):
            href = anchor.get("href") or ""
            absolute = urljoin(listing_url, href)
            if not absolute.startswith("http"):
                continue
            if absolute in seen:
                continue
            # Filter by same domain and reasonable path depth to avoid footer/header links
            netloc = urlparse(absolute).netloc.lower()
            if netloc != domain:
                continue
            path = urlparse(absolute).path or ""
            segments = [s for s in path.split("/") if s]
            if len(segments) <= 4:
                # skip shallow links (home, section, tag pages)
                continue
            seen.add(absolute)
            try:
                doc, log = self.run(absolute)
            except Exception:
                continue
            if doc is not None:
                docs.append((doc, log))
            if len(docs) >= max(1, int(limit)):
                break
        return docs

    def _extract_pub_date(self, node: Optional[BeautifulSoup], root: Optional[BeautifulSoup] = None) -> Optional[str]:
        """Best-effort extraction of publication date from common meta tags or time tags.

        The function first searches inside `node` (article fragment), then falls
        back to `root` (full soup) to locate <meta> tags in the document head.
        """
        try:
            if node is not None:
                metas = node.select("meta[property='article:published_time'], meta[name='date'], meta[name='pubdate']")
                for m in metas:
                    if m.has_attr('content'):
                        return (m.get('content') or '').strip()
                t = node.select_one('time[datetime]')
                if t and t.has_attr('datetime'):
                    return (t.get('datetime') or '').strip()
                for sel in ('.pubdate', '.published', '.date', '.article-date'):
                    el = node.select_one(sel)
                    if el and el.get_text(strip=True):
                        return el.get_text(strip=True)

            # Fallback: check the full document for head/meta tags
            if root is not None:
                metas = root.select("meta[property='article:published_time'], meta[name='date'], meta[name='pubdate'], meta[name='publication_date']")
                for m in metas:
                    if m.has_attr('content'):
                        return (m.get('content') or '').strip()
                t = root.select_one('time[datetime]')
                if t and t.has_attr('datetime'):
                    return (t.get('datetime') or '').strip()
        except Exception:
            return None
        return None
