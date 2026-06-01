from __future__ import annotations

"""
Technical Resources Explorer

Breadth-first in-memory crawler to collect technical resources from a site.
- Produces a Markdown aggregation of discovered HTML resources.
- Can delegate PDF acquisition to `PdfDownloader` when configured (this
    component may perform binary downloads).

Note: when used in a PDF-first workflow this module will emit discovery
records via `DiscoveryWriter`; otherwise it operates in pure memory mode.
"""

from collections import deque
from typing import Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from agriconnect.core.schemas import RawDocument
from agriconnect.core.scraper_utils import extract_markdown_from_html

from ..shared.base_scraper import BaseScraper
from .pdf_downloader import PdfDownloader
from .pdf_discovery import DiscoveryWriter


class TechnicalResourcesExplorer(BaseScraper):
    """Breadth-first technical crawler in pure memory mode."""

    name = "technical_resources_explorer"
    version = "2.0.0"

    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config=config)
        cfg = config or {}
        self.max_depth = int(cfg.get("max_depth", 2))
        self.max_pages = int(cfg.get("max_pages", 25))
        # maximum cumulative bytes of downloaded content before aborting the crawl
        self.max_memory_limit = int(cfg.get("max_memory_limit", 50 * 1024 * 1024))  # 50 MB default
        # Keep native type (list or string) from YAML/config.
        self.preferred_selectors = cfg.get(
            "preferred_selectors", "main, article, #content, .post-content"
        )
        self.pdf_downloader = PdfDownloader(config=config or {})
        # discovery writer for PDF-first workflow
        self.discovery_output_path = str(cfg.get("discovery_output_path") or "data/pdf_discovery/technical_discovery.ndjson")
        try:
            self.discovery_writer = DiscoveryWriter(self.discovery_output_path)
        except Exception:
            self.discovery_writer = None

    def _title_from_html(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        return (soup.title.string or "Technical Resource").strip() if soup.title and soup.title.string else "Technical Resource"

    def _collect_internal_links(self, html: str, base_url: str):
        soup = BeautifulSoup(html, "html.parser")
        base_netloc = urlparse(base_url).netloc
        html_links = []
        pdf_links = []
        for anchor in soup.select("a[href]"):
            href = (anchor.get("href") or "").strip()
            if not href:
                continue
            # normalize: resolve relative, then drop fragment
            target = urljoin(base_url, href).split("#", 1)[0]
            parsed = urlparse(target)
            if parsed.netloc != base_netloc:
                continue
            # skip mailto/tel
            if parsed.scheme not in ("http", "https"):
                continue
            # delegate PDFs
            if parsed.path.lower().endswith('.pdf'):
                pdf_links.append(target)
                continue
            html_links.append(target)
        return html_links, pdf_links

    def scrape(self, url: str) -> Tuple[Optional[RawDocument], Dict]:
        visited = set([url])
        queue = deque([(url, 0)])
        markdown_blocks = []
        total_bytes = 0
        last_http_status = None
        pages = 0
        first_title = "Technical Resource"

        while queue and pages < self.max_pages:
            current, depth = queue.popleft()
            if depth > self.max_depth:
                continue
            response = self._request(current)
            last_http_status = response.status_code
            html = response.text
            total_bytes += len(response.content or b"")
            # enforce cumulative memory limit
            if total_bytes > self.max_memory_limit:
                break
            pages += 1

            # try to focus on main/article fragment if present to reduce boilerplate
            soup = BeautifulSoup(html, "html.parser")
            target_html = self._prepare_markdown_html(
                html,
                base_url=current,
                preferred_selectors=self.preferred_selectors,
            )

            if pages == 1:
                first_title = self._title_from_html(html)

            md = extract_markdown_from_html(target_html, include_links=True)
            quality = self._assess_extraction_quality(md, html)
            if quality.get("blocked_by_waf"):
                continue
            if md.strip():
                markdown_blocks.append(f"## Page {pages}: {current}\n\n{md}")

            # collect links and pdfs separately
            html_links, pdf_links = self._collect_internal_links(html, current)

            # handle PDF links by emitting discovery records instead of downloading
            for pdf_url in pdf_links:
                try:
                    if self.discovery_writer is not None:
                        self.discovery_writer.write({
                            "pdf_url": pdf_url,
                            "parent_page": current,
                            "source": "technical_resources_explorer",
                        })
                    # include a small placeholder mention in the markdown
                    markdown_blocks.append(f"## PDF queued: {pdf_url}\n\n(queued for downstream PDF ingestion)")
                except Exception:
                    # fallback: no-op if writer fails
                    markdown_blocks.append(f"## PDF: {pdf_url}\n\n(queued_failed)")
                pages += 1
                if total_bytes > self.max_memory_limit:
                    break

            if total_bytes > self.max_memory_limit:
                break

            for nxt in html_links:
                if nxt in visited:
                    continue
                visited.add(nxt)
                queue.append((nxt, depth + 1))

        markdown = "\n\n---\n\n".join(markdown_blocks)
        if not markdown.strip():
            return None, {
                "http_status": last_http_status,
                "bytes_downloaded": total_bytes,
                "raw_payload": "",
            }

        doc = self._build_document(
            url=url,
            title=first_title,
            markdown=markdown,
            metadata={
                "source_type": "technical_site",
                    "source_domain": self._source_domain(url),
                    "publication_date": None,
                    "language": None,
                    "extraction_method": "multi_page_html_to_markdown",
                "pages_crawled": pages,
                "max_depth": self.max_depth,
                    "memory_limit_bytes": self.max_memory_limit,
            },
            language=None,
        )
        return doc, {
            "http_status": last_http_status,
            "bytes_downloaded": total_bytes,
            "raw_payload": markdown,
        }

    def scrape_legacy(self, url: str) -> Dict:
        doc, log = self.run(url)
        if doc is None:
            return {
                "status": "failed",
                "url": url,
                "source_type": "technical_site",
                "error": log.error_trace or "crawl_failed",
            }
        return {
            "status": "success",
            "url": url,
            "source_type": "technical_site",
            "title": doc.title,
            "content": doc.content_markdown,
            "metadata": doc.metadata,
            "scraper_log": log.model_dump(),
        }
