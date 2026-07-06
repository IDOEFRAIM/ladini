from __future__ import annotations

"""
Institutional PDF Harvester

Generic harvester for institutional reports. Designed to discover PDF
candidates and, when configured, delegate PDF acquisition to `PdfDownloader`.
- Operates in a stateless, in-memory mode by default and can emit discovery
    records via `DiscoveryWriter`.
- Does NOT perform low-level binary validation itself; ingestion manages
    download and validation.
"""

import re
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
import requests

from agriconnect.core.schemas import RawDocument

from ..shared.base_scraper import BaseScraper
from .pdf_discovery import (
    DiscoveryWriter,
    is_pdf_candidate,
    is_wordpress_pdf,
    normalize_pdf_url,
    surrounding_text_for_anchor,
)


class InstitutionalPdfHarvester(BaseScraper):
    """Generic institutional PDF report harvester using PdfDownloader."""

    name = "institutional_pdf_harvester"
    version = "2.0.0"

    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config=config)
        cfg = config or {}
        selectors = cfg.get("selectors") if isinstance(cfg.get("selectors"), dict) else {}
        engine_cfg = cfg.get("engine_config") if isinstance(cfg.get("engine_config"), dict) else {}
        self.base_url = cfg.get("base_url", "")
        self.search_urls = list(cfg.get("search_urls") or [])
        include_patterns = cfg.get("include_patterns") or engine_cfg.get("include_patterns") or [r"report", r"bulletin", r"pdf"]
        exclude_patterns = cfg.get("exclude_patterns") or engine_cfg.get("exclude_patterns") or [r"login", r"signup", r"share"]
        self.include_patterns = [re.compile(p, re.I) for p in include_patterns]
        self.exclude_patterns = [re.compile(p, re.I) for p in exclude_patterns]
        self.max_pages = int(cfg.get("max_pages", 400))
        self.max_pdfs = int(cfg.get("max_pdfs", 200))
        self.max_depth = int(cfg.get("max_depth", 3))
        self.branch_prune_threshold = int(cfg.get("branch_prune_threshold", 50))
        self.discovery_output_path = str(cfg.get("discovery_output_path") or "data/pdf_discovery/institutional_discovery.ndjson")
        self.deep_extraction_enabled = bool(engine_cfg.get("deep_extraction") or cfg.get("deep_extraction", False))

        self.circuit_threshold = int(cfg.get("circuit_threshold", 3))
        self.circuit_quarantine_minutes = int(cfg.get("circuit_quarantine_minutes", 30))
        self._domain_failures: Dict[str, int] = {}
        self._domain_quarantine_until: Dict[str, datetime] = {}

        self.listing_link_selector = selectors.get("listing_links") or "a[href]"
        self.pdf_link_selector = selectors.get("pdf_links") or self.listing_link_selector
        self.download_keywords = list(engine_cfg.get("download_keywords") or ["download", "telecharger", "télécharger"])

    def _matches_download_keyword(self, value: str) -> bool:
        low = (value or "").lower()
        return any(keyword.lower() in low for keyword in self.download_keywords)

    def _is_candidate_link(self, href: str, text: str) -> bool:
        low = f"{href} {text}".lower()
        if any(p.search(low) for p in self.exclude_patterns):
            return False
        # Keep pagination/archive traversal for deep crawling of historical bulletins.
        pagination_tokens = ("suivant", "next", "archive", "older", "page=")
        if any(tok in low for tok in pagination_tokens):
            return True
        if self.deep_extraction_enabled and self._matches_download_keyword(low):
            return True
        if href.lower().endswith(".pdf"):
            return True
        return any(p.search(low) for p in self.include_patterns)

    def _is_domain_quarantined(self, domain: str) -> bool:
        until = self._domain_quarantine_until.get(domain)
        if not until:
            return False
        if until <= datetime.now(timezone.utc):
            self._domain_quarantine_until.pop(domain, None)
            self._domain_failures.pop(domain, None)
            return False
        return True

    def _record_http_status(self, domain: str, status: int) -> None:
        if status in (403, 429):
            failures = int(self._domain_failures.get(domain, 0)) + 1
            self._domain_failures[domain] = failures
            if failures >= self.circuit_threshold:
                self._domain_quarantine_until[domain] = datetime.now(timezone.utc) + timedelta(minutes=self.circuit_quarantine_minutes)
        elif status < 400:
            self._domain_failures.pop(domain, None)

    def _safe_page_get(self, page_url: str) -> Optional[requests.Response]:
        domain = urlparse(page_url).netloc.lower()
        if self._is_domain_quarantined(domain):
            return None
        try:
            response = self._request(page_url)
            self._record_http_status(domain, response.status_code)
            return response
        except requests.HTTPError as exc:
            status = int(getattr(exc.response, "status_code", 0) or 0)
            if status:
                self._record_http_status(domain, status)
            return None
        except Exception:
            return None

    def _branch_signature(self, page_url: str) -> str:
        parsed = urlparse(page_url)
        parts = [p for p in (parsed.path or "").split("/") if p]
        return "/" + "/".join(parts[:2]) if parts else "/"

    def _extract_page_title(self, soup: BeautifulSoup) -> str:
        title_node = soup.select_one("h1") or soup.select_one("title")
        if title_node and title_node.get_text(strip=True):
            return title_node.get_text(strip=True)
        return "Institutional Page"

    def _extract_page_date(self, soup: BeautifulSoup) -> Optional[str]:
        node = (
            soup.select_one("meta[property='article:published_time']")
            or soup.select_one("meta[name='date']")
            or soup.select_one("time[datetime]")
        )
        if not node:
            return None
        value = node.get("content") or node.get("datetime") or node.get_text(" ", strip=True)
        return self._normalize_publication_date(value)

    def _discover_recursive(self, start_url: str, writer: DiscoveryWriter) -> int:
        root = normalize_pdf_url(start_url)
        root_netloc = urlparse(root).netloc.lower()
        queue: Deque[Tuple[str, int]] = deque([(root, 0)])
        visited: Set[str] = set()
        branch_empty_counts: Dict[str, int] = {}
        total_new = 0

        while queue and len(visited) < self.max_pages and total_new < self.max_pdfs:
            current, depth = queue.popleft()
            if current in visited:
                continue
            if depth > self.max_depth:
                continue
            visited.add(current)

            response = self._safe_page_get(current)
            if response is None:
                continue

            soup = BeautifulSoup(response.text, "html.parser")
            page_title = self._extract_page_title(soup)
            page_date = self._extract_page_date(soup)
            branch = self._branch_signature(current)
            page_new = 0

            anchors = list(soup.select(str(self.pdf_link_selector)))
            anchors.sort(key=lambda a: 0 if is_wordpress_pdf(urljoin(current, str(a.get("href") or "")) or "") else 1)

            for anchor in anchors:
                href = (anchor.get("href") or "").strip()
                if not href:
                    continue
                absolute = normalize_pdf_url(urljoin(current, href).split("#", 1)[0])
                low = absolute.lower()
                label = (anchor.get_text(" ", strip=True) or "").lower()
                has_keyword = self._matches_download_keyword(low) or self._matches_download_keyword(label)
                if not (is_pdf_candidate(absolute) or has_keyword):
                    continue
                if not is_pdf_candidate(absolute):
                    # Deep extraction mode: treat this as a detail/download page and crawl it recursively.
                    continue
                if writer.has_seen(absolute):
                    continue

                record = {
                    "pdf_url": normalize_pdf_url(absolute),
                    "parent_page_url": current,
                    "context": {
                        "page_title": page_title,
                        "anchor_text": anchor.get_text(" ", strip=True) or None,
                        "surrounding_text": surrounding_text_for_anchor(anchor, max_chars=300),
                    },
                    "metadata": {
                        "site_name": root_netloc,
                        "discovery_method": "crawl",
                        "extracted_date": page_date,
                    },
                    "resolver_meta": {
                        "resolution_method": "direct_candidate",
                        "resolved_from": current,
                    },
                }
                if writer.write(record):
                    total_new += 1
                    page_new += 1
                    if total_new >= self.max_pdfs:
                        break

            if page_new == 0:
                branch_empty_counts[branch] = int(branch_empty_counts.get(branch, 0)) + 1
            else:
                branch_empty_counts[branch] = 0

            if depth >= self.max_depth:
                continue
            if branch_empty_counts.get(branch, 0) >= self.branch_prune_threshold:
                continue

            for anchor in soup.select(str(self.listing_link_selector)):
                href = (anchor.get("href") or "").strip()
                if not href:
                    continue
                nxt = normalize_pdf_url(urljoin(current, href).split("#", 1)[0])
                parsed = urlparse(nxt)
                if parsed.scheme not in ("http", "https"):
                    continue
                if parsed.netloc.lower() != root_netloc:
                    continue
                if is_pdf_candidate(nxt):
                    continue
                if nxt in visited:
                    continue
                if not self._is_candidate_link(nxt, anchor.get_text(" ", strip=True)):
                    continue
                queue.append((nxt, depth + 1))

        return total_new

    def scrape(self, url: str) -> Tuple[Optional[RawDocument], Dict]:
        writer = DiscoveryWriter(self.discovery_output_path)
        discovered = self._discover_recursive(start_url=url, writer=writer)
        markdown = (
            "# Institutional PDF Discovery\n\n"
            f"- seed_url: {url}\n"
            f"- mapping_file: {self.discovery_output_path}\n"
            f"- discovered_records: {discovered}\n"
            f"- scanned_at: {datetime.now(timezone.utc).isoformat()}\n"
        )
        doc = self._build_document(
            url=url,
            title="Institutional PDF Discovery",
            markdown=markdown,
            metadata={
                "source_type": "institutional_pdf_discovery",
                "source_domain": self._source_domain(url),
                "discovery_output_path": self.discovery_output_path,
                "discovery_count": discovered,
                "extraction_method": "pdf_discovery_mapping",
            },
            language=None,
        )
        return doc, {
            "http_status": 200,
            "bytes_downloaded": 0,
            "raw_payload": markdown,
            "discovery_count": discovered,
            "mapping_file": self.discovery_output_path,
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
