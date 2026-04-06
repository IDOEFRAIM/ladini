from __future__ import annotations

"""
Institutional PDF Harvester (V2)

Shared, service-first engine for institutional report scraping.
- Stateless scraper contract (scrape -> RawDocument + meta)
- Zero disk side effects
- PDF extraction delegated to PdfDownloader
- Config-driven domain/listing patterns
"""

import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from agriconnect.core.schemas import RawDocument
from agriconnect.core.scraper_utils import extract_markdown_from_html

from .base import BaseScraper
from .pdf_downloader import PdfDownloader
from .registry import register_scraper


@register_scraper("institutional_pdf")
class InstitutionalPdfHarvester(BaseScraper):
    """Generic institutional PDF report harvester using PdfDownloader."""

    name = "institutional_pdf_harvester"
    version = "2.0.0"

    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config=config)
        cfg = config or {}
        self.base_url = cfg.get("base_url", "")
        self.search_urls = list(cfg.get("search_urls") or [])
        self.include_patterns = [re.compile(p, re.I) for p in (cfg.get("include_patterns") or [r"report", r"bulletin", r"mise-jour", r"perspectives", r"key-message"]) ]
        self.exclude_patterns = [re.compile(p, re.I) for p in (cfg.get("exclude_patterns") or [r"login", r"signup", r"share", r"whatsapp", r"facebook"]) ]
        self.max_pages = int(cfg.get("max_pages", 30))
        self.max_pdfs = int(cfg.get("max_pdfs", 20))
        self.pdf_downloader = PdfDownloader(config=cfg)

    def _is_candidate_link(self, href: str, text: str) -> bool:
        low = f"{href} {text}".lower()
        if any(p.search(low) for p in self.exclude_patterns):
            return False
        if href.lower().endswith(".pdf"):
            return True
        return any(p.search(low) for p in self.include_patterns)

    def _collect_listing_links(self, listing_url: str) -> List[str]:
        response = self._request(listing_url)
        soup = BeautifulSoup(response.text, "html.parser")
        links: List[str] = []
        domain = urlparse(listing_url).netloc.lower()
        for a in soup.select("a[href]"):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            absolute = urljoin(listing_url, href).split("#", 1)[0]
            parsed = urlparse(absolute)
            if parsed.scheme not in ("http", "https"):
                continue
            if parsed.netloc.lower() != domain:
                continue
            text = a.get_text(" ", strip=True)
            if self._is_candidate_link(absolute, text):
                links.append(absolute)
            if len(links) >= self.max_pages:
                break
        # dedupe preserve order
        out: List[str] = []
        seen = set()
        for u in links:
            if u in seen:
                continue
            seen.add(u)
            out.append(u)
        return out

    def _collect_pdf_links(self, page_url: str) -> List[str]:
        response = self._request(page_url)
        html = response.text
        soup = BeautifulSoup(html, "html.parser")

        pdf_links: List[str] = []
        for a in soup.select("a[href]"):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            absolute = urljoin(page_url, href).split("#", 1)[0]
            low = absolute.lower()
            label = (a.get_text(" ", strip=True) or "").lower()
            if low.endswith(".pdf") or "download" in low or "download" in label:
                pdf_links.append(absolute)

        # if page itself is a direct PDF
        if page_url.lower().endswith(".pdf"):
            pdf_links.insert(0, page_url)

        out: List[str] = []
        seen = set()
        for u in pdf_links:
            if u in seen:
                continue
            seen.add(u)
            out.append(u)
        return out[: self.max_pdfs]

    def scrape(self, url: str) -> Tuple[Optional[RawDocument], Dict]:
        listing_urls = [url]
        report_pages = self._collect_listing_links(url)
        if not report_pages:
            report_pages = [url]

        markdown_blocks: List[str] = []
        bytes_downloaded = 0
        extracted_count = 0

        for page in report_pages[: self.max_pages]:
            pdf_urls = self._collect_pdf_links(page)
            for pdf_url in pdf_urls:
                pdf_doc, pdf_log = self.pdf_downloader.run(pdf_url)
                if pdf_doc is None:
                    continue
                extracted_count += 1
                bytes_downloaded += int(getattr(pdf_log, "bytes_downloaded", 0) or 0)
                markdown_blocks.append(f"## Source PDF: {pdf_url}\n\n{pdf_doc.content_markdown}")
                if extracted_count >= self.max_pdfs:
                    break
            if extracted_count >= self.max_pdfs:
                break

        if not markdown_blocks:
            # fallback single-page html extraction
            response = self._request(url)
            html = response.text
            markdown_html = self._prepare_markdown_html(
                html,
                base_url=url,
                preferred_selectors="main, article, #content, .post-content",
            )
            markdown = extract_markdown_from_html(markdown_html, include_links=True)
            quality = self._assess_extraction_quality(markdown, html)
            if quality.get("blocked_by_waf") or not markdown.strip():
                return None, {
                    "http_status": response.status_code,
                    "bytes_downloaded": len(response.content or b""),
                    "raw_payload": html,
                    "blocked_by_waf": bool(quality.get("blocked_by_waf")),
                }

            doc = self._build_document(
                url=url,
                title="Institutional Resource",
                markdown=markdown,
                metadata={
                    "source_type": "institutional_document",
                    "source_domain": self._source_domain(url),
                    "publication_date": None,
                    "language": self._extract_language_code(BeautifulSoup(html, "html.parser")),
                    "extraction_method": "html_to_markdown",
                    "partial_extraction": bool(quality.get("partial_extraction")),
                    "listing_urls": listing_urls,
                },
                language=self._extract_language_code(BeautifulSoup(html, "html.parser")),
            )
            return doc, {
                "http_status": response.status_code,
                "bytes_downloaded": len(response.content or b""),
                "raw_payload": html,
                "partial_extraction": bool(quality.get("partial_extraction")),
            }

        markdown = "# Institutional Reports\n\n" + "\n\n---\n\n".join(markdown_blocks)
        quality = self._assess_extraction_quality(markdown, markdown)
        doc = self._build_document(
            url=url,
            title="Institutional Reports",
            markdown=markdown,
            metadata={
                "source_type": "institutional_document",
                "source_domain": self._source_domain(url),
                "publication_date": None,
                "language": None,
                "extraction_method": "pdf_parsing",
                "partial_extraction": bool(quality.get("partial_extraction")),
                "reports_extracted": extracted_count,
                "listing_urls": listing_urls,
            },
            language=None,
        )
        return doc, {
            "http_status": 200,
            "bytes_downloaded": bytes_downloaded,
            "raw_payload": markdown,
            "partial_extraction": bool(quality.get("partial_extraction")),
        }

    def run_harvest(self, start_url: Optional[str] = None) -> Dict:
        target = start_url or (self.search_urls[0] if self.search_urls else self.base_url)
        doc, log = self.run(target)
        if doc is None:
            return {"status": "ERROR", "results": [], "error": log.error_trace or "harvest_failed"}
        return {
            "status": "SUCCESS",
            "results": [
                {
                    "title": doc.title,
                    "url": doc.url,
                    "char_count": len(doc.content_markdown or ""),
                    "metadata": doc.metadata,
                }
            ],
            "error": None,
        }
