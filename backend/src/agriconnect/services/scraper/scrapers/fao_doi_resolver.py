from __future__ import annotations

"""
FAO DOI Resolver (V2)

Resolves FAO DOIs/URLs, prefers PDF extraction when available,
falls back to HTML->Markdown otherwise. Returns `RawDocument` + meta.
"""

import re
from typing import Dict, Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from agriconnect.core.schemas import RawDocument
from agriconnect.core.scraper_utils import extract_markdown_from_html

from .base import BaseScraper
from .pdf_downloader import PdfDownloader
from .registry import register_scraper


@register_scraper("fao", "fao_doi", "doi")
class FaoDoiResolver(BaseScraper):
    """Resolve FAO DOI and return in-memory markdown-ready content."""

    name = "fao_doi_resolver"
    version = "2.0.0"

    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config=config)
        self.pdf_downloader = PdfDownloader(config=config or {})

    def resolve_doi(self, doi_url: str) -> Optional[str]:
        try:
            self._before_fetch(doi_url)
            response = self.session.head(doi_url, allow_redirects=True, timeout=self.policy.timeout)
            response.raise_for_status()
            return response.url
        except Exception:
            return None

    @staticmethod
    def extract_fao_code(url: str) -> Optional[str]:
        patterns = [
            r"/([a-z]{2}\d{4,}[a-z]{2})",
            r"/c/([A-Z0-9]+)",
            r"\.org/3/([a-z]{2}\d{4,}[a-z]{2})",
        ]
        for pattern in patterns:
            match = re.search(pattern, url, re.IGNORECASE)
            if match:
                return match.group(1).lower()
        return None

    @staticmethod
    def construct_pdf_url(fao_code: str) -> str:
        return f"https://www.fao.org/3/{fao_code}/{fao_code}.pdf"

    @staticmethod
    def extract_language_from_fao_code(fao_code: Optional[str]) -> Optional[str]:
        """Extract language suffix from FAO code when present (e.g., cb4567fr -> fr)."""
        if not fao_code:
            return None
        code = fao_code.strip().lower()
        if len(code) >= 2 and code[-2:].isalpha():
            return code[-2:]
        return None

    def _scrape_publication_page(self, url: str) -> Dict:
        response = self._request(url)
        html = response.text
        soup = BeautifulSoup(html, "html.parser")

        title_elem = soup.select_one("h1") or soup.select_one("title")
        title = title_elem.get_text(strip=True) if title_elem else "Publication FAO"

        pdf_links = []
        for anchor in soup.select("a[href]"):
            href = anchor.get("href") or ""
            href_l = href.lower()
            if href_l.endswith(".pdf") or "download" in href_l:
                pdf_links.append(urljoin(url, href))

        markdown_html = self._prepare_markdown_html(
            html,
            base_url=url,
            preferred_selectors="main, article, #content, .post-content",
        )
        markdown = extract_markdown_from_html(markdown_html, include_links=True)
        return {
            "title": title,
            "pdf_links": pdf_links,
            "markdown": markdown,
            "http_status": response.status_code,
            "bytes_downloaded": len(response.content or b""),
            "raw_payload": html,
            "language": self._extract_language_code(soup),
        }

    def scrape(self, url: str) -> Tuple[Optional[RawDocument], Dict]:
        canonical_url = self.resolve_doi(url)
        if not canonical_url:
            return None, {"http_status": None, "bytes_downloaded": 0, "raw_payload": ""}

        page = self._scrape_publication_page(canonical_url)
        fao_code = self.extract_fao_code(canonical_url)
        fao_language = self.extract_language_from_fao_code(fao_code)

        # Prefer PDF content when available.
        pdf_candidates = []
        if fao_code:
            pdf_candidates.append(self.construct_pdf_url(fao_code))
        pdf_candidates.extend(page.get("pdf_links", []) or [])

        # Deduplicate while preserving order.
        unique_pdf_candidates = []
        seen = set()
        for candidate in pdf_candidates:
            if not candidate:
                continue
            key = candidate.strip()
            if key in seen:
                continue
            seen.add(key)
            unique_pdf_candidates.append(key)

        for pdf_url in unique_pdf_candidates:
            pdf_doc, pdf_log = self.pdf_downloader.run(pdf_url)
            if pdf_doc is None:
                continue

            doc = self._build_document(
                url=canonical_url,
                title=page.get("title") or pdf_doc.title,
                markdown=pdf_doc.content_markdown,
                metadata={
                    "source_type": "fao_publication",
                    "source_domain": self._source_domain(canonical_url),
                    "publication_date": None,
                    "language": pdf_doc.language or fao_language or page.get("language"),
                    "doi": url,
                    "canonical_url": canonical_url,
                    "fao_code": fao_code,
                    "fao_language": fao_language,
                    "extraction_method": "pdf_parsing",
                    "pdf_url_used": pdf_url,
                },
                language=pdf_doc.language or fao_language or page.get("language"),
            )
            return doc, {
                "http_status": page.get("http_status"),
                "bytes_downloaded": int(page.get("bytes_downloaded", 0)) + int(pdf_log.bytes_downloaded),
                "raw_payload": page.get("raw_payload", ""),
            }

        # Fallback to HTML markdown.
        markdown = page.get("markdown", "")
        quality = self._assess_extraction_quality(markdown, str(page.get("raw_payload", "")))
        if quality.get("blocked_by_waf"):
            return None, {
                "http_status": page.get("http_status"),
                "bytes_downloaded": int(page.get("bytes_downloaded", 0)),
                "raw_payload": page.get("raw_payload", ""),
                "blocked_by_waf": True,
            }
        if not markdown.strip():
            return None, {
                "http_status": page.get("http_status"),
                "bytes_downloaded": int(page.get("bytes_downloaded", 0)),
                "raw_payload": page.get("raw_payload", ""),
            }

        doc = self._build_document(
            url=canonical_url,
            title=page.get("title") or "Publication FAO",
            markdown=markdown,
            metadata={
                "source_type": "fao_publication",
                "source_domain": self._source_domain(canonical_url),
                "publication_date": None,
                "language": page.get("language") or fao_language,
                "doi": url,
                "canonical_url": canonical_url,
                "fao_code": fao_code,
                "fao_language": fao_language,
                "extraction_method": "html_fallback",
                "pdf_links": page.get("pdf_links", []),
                "partial_extraction": bool(quality.get("partial_extraction")),
            },
            language=page.get("language") or fao_language,
        )
        return doc, {
            "http_status": page.get("http_status"),
            "bytes_downloaded": int(page.get("bytes_downloaded", 0)),
            "raw_payload": page.get("raw_payload", ""),
            "partial_extraction": bool(quality.get("partial_extraction")),
        }
