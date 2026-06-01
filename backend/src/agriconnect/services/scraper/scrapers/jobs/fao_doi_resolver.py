from __future__ import annotations

"""
FAO DOI Resolver

Resolves FAO DOIs/URLs and prefers PDF bitstreams when available. This
component may delegate PDF fetching to `PdfDownloader` and will fall back to
HTML->Markdown conversion when direct PDFs are not available.

Note: binary validation and content checks are performed by the ingestion
downloader (`PdfDownloader`); this resolver emits discovery records when
configured to do so.
"""

import re
from typing import Dict, Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from agriconnect.core.schemas import RawDocument
from agriconnect.core.scraper_utils import extract_markdown_from_html

from ..shared.base_scraper import BaseScraper
from .pdf_downloader import PdfDownloader
from .pdf_discovery import DiscoveryWriter


class FaoDoiResolver(BaseScraper):
    """Resolve FAO DOI and return in-memory markdown-ready content."""

    name = "fao_doi_resolver"
    version = "2.0.0"

    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config=config)
        self.pdf_downloader = PdfDownloader(config=config or {})
        cfg = config or {}
        self.discovery_output_path = str(cfg.get("discovery_output_path") or "data/pdf_discovery/fao_discovery.ndjson")
        try:
            self.discovery_writer = DiscoveryWriter(self.discovery_output_path)
        except Exception:
            self.discovery_writer = None

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

        # If DOI resolution lands directly on a PDF, queue it immediately.
        direct_pdf = canonical_url.lower().endswith(".pdf") or "/bitstreams/" in canonical_url.lower()
        if direct_pdf:
            fao_code = self.extract_fao_code(url) or self.extract_fao_code(canonical_url)
            fao_language = self.extract_language_from_fao_code(fao_code)
            try:
                if self.discovery_writer is not None:
                    self.discovery_writer.write({
                        "pdf_url": canonical_url,
                        "parent_page_url": canonical_url,
                        "source": "fao_doi_resolver",
                        "metadata": {"fao_code": fao_code, "language": fao_language},
                    })
            except Exception:
                pass

            placeholder_md = f"# FAO PDF queued\n\nURL: {canonical_url}\n\nQueued for downstream PDF ingestion."
            doc = self._build_document(
                url=canonical_url,
                title="FAO Publication (PDF queued)",
                markdown=placeholder_md,
                metadata={
                    "source_type": "fao_publication",
                    "source_domain": self._source_domain(canonical_url),
                    "doi": url,
                    "canonical_url": canonical_url,
                    "fao_code": fao_code,
                    "fao_language": fao_language,
                    "extraction_method": "queued_pdf_direct",
                    "pdf_url_used": canonical_url,
                },
                language=fao_language,
            )
            return doc, {"http_status": 200, "bytes_downloaded": 0, "raw_payload": ""}

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
            # Queue PDF for downstream ingestion instead of inline download
            try:
                if self.discovery_writer is not None:
                    self.discovery_writer.write({
                        "pdf_url": pdf_url,
                        "parent_page_url": canonical_url,
                        "source": "fao_doi_resolver",
                        "metadata": {"fao_code": fao_code, "language": fao_language},
                    })
            except Exception:
                pass

            # Return a lightweight placeholder document indicating queuing
            placeholder_md = f"# FAO PDF queued\n\nURL: {pdf_url}\n\nQueued for downstream PDF ingestion."
            doc = self._build_document(
                url=canonical_url,
                title=page.get("title") or "FAO Publication (PDF queued)",
                markdown=placeholder_md,
                metadata={
                    "source_type": "fao_publication",
                    "source_domain": self._source_domain(canonical_url),
                    "doi": url,
                    "canonical_url": canonical_url,
                    "fao_code": fao_code,
                    "fao_language": fao_language,
                    "extraction_method": "queued_pdf",
                    "pdf_url_used": pdf_url,
                },
                language=fao_language or page.get("language"),
            )
            return doc, {
                "http_status": page.get("http_status"),
                "bytes_downloaded": int(page.get("bytes_downloaded", 0)),
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


def _cli_main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run FaoDoiResolver on a single DOI/URL and optionally emit discovery NDJSON.")
    parser.add_argument("url", help="DOI or URL to resolve (e.g., FAO DOI or FAO publication URL)")
    parser.add_argument("--discovery-output", dest="discovery_output", default=None,
                        help="Path to discovery NDJSON output (overrides scraper config)")
    parser.add_argument("--ignore-robots", dest="ignore_robots", action="store_true",
                        help="Ignore robots.txt for this run")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    cfg = {}
    if args.discovery_output:
        cfg["discovery_output_path"] = args.discovery_output
    if args.ignore_robots:
        cfg.setdefault("network_params", {})["respect_robots"] = False

    scraper = FaoDoiResolver(config=cfg)

    # Use run() to obtain both document and log
    try:
        doc, log = scraper.run(args.url)
    except Exception as e:
        print("Error running scraper:", e)
        return 2

    if doc is None:
        print("Scrape failed: log=", log)
        return 1

    print("Title:", doc.title)
    print("URL:", doc.url)
    print("MD length:", len(doc.content_markdown or ""))
    print("Meta keys:", list(doc.metadata.keys()))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
