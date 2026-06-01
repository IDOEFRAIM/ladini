from __future__ import annotations

"""
News PDF Discovery (discovery-only)

This module implements `NewsScraper` as a discovery-only, in-memory crawler.
- Scans HTML pages to emit PDF candidate discovery records (generator-based).
- Does NOT perform binary downloads, magic-number checks, or content validation.
- Binary acquisition and strict validation are the responsibility of
    the ingestion layer (`PdfDownloader` / `pdf_downloader.py`).

Contract: `_discover_recursive(url)` yields discovery records (dict). ``scrape``
will write those records via `DiscoveryWriter`.
"""

from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, Optional, Set, Tuple
from urllib.parse import urlparse, urljoin

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


class NewsScraper(BaseScraper):
    """PDF discovery agent for news pages with recursive crawl support."""

    name = "news_scraper"
    version = "2.0.0"

    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config=config)
        cfg = config or {}
        selectors = cfg.get("selectors") if isinstance(cfg.get("selectors"), dict) else {}
        engine_cfg = cfg.get("engine_config") if isinstance(cfg.get("engine_config"), dict) else {}

        # discovery-only scraper: no heavy article selectors
        self.discovery_output_path = str(cfg.get("discovery_output_path") or "data/pdf_discovery/news_discovery.ndjson")
        self.max_depth = int(cfg.get("max_depth", 3))
        self.max_pages = int(cfg.get("max_pages", 400))
        # Optional cap for supervised runs (0 = unlimited)
        self.max_pdfs = int(cfg.get("max_pdfs", 0) or 0)
        self.branch_prune_threshold = int(cfg.get("branch_prune_threshold", 50))
        # Note: PDF size/validation removed — ingestion is responsible for binary checks
        self.deep_extraction_enabled = bool(engine_cfg.get("deep_extraction") or cfg.get("deep_extraction", False))
        self.download_keywords = list(
            engine_cfg.get("download_keywords")
            or ["download", "telecharger", "télécharger", "document", "bitstream", "pdf"]
        )

        # circuit-breaker moved to BaseScraper

    def _matches_download_keyword(self, value: str) -> bool:
        low = (value or "").lower()
        return any(keyword.lower() in low for keyword in self.download_keywords)

    def scrape(self, url: str) -> Tuple[Optional[RawDocument], Dict]:
        writer = DiscoveryWriter(self.discovery_output_path)
        new_records = 0
        for record in self._discover_recursive(url=url):
            if writer.write(record):
                new_records += 1
                if self.max_pdfs > 0 and new_records >= self.max_pdfs:
                    break
        discovered = new_records

        markdown = (
            "# News PDF Discovery\n\n"
            f"- seed_url: {url}\n"
            f"- mapping_file: {self.discovery_output_path}\n"
            f"- discovered_records: {discovered}\n"
            f"- scanned_at: {datetime.now(timezone.utc).isoformat()}\n"
        )
        doc = self._build_document(
            url=url,
            title="News PDF Discovery",
            markdown=markdown,
            metadata={
                "source_type": "news_pdf_discovery",
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

    # Circuit-breaker helpers are provided by BaseScraper; do not reimplement here.

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

    def _resolve_direct_pdf_url(self, candidate_url: str, max_hops: int = 3) -> Tuple[Optional[str], Dict]:
        """
        Simplified resolution: only detect direct iframe/embed/object targets or
        anchors/buttons that look like download triggers. Do not perform binary
        validation here — that belongs to the ingestion/pdf_downloader.
        """
        # Non-recursive, static extraction: fetch page and look for embedded PDF viewers
        page_response = self._safe_page_get(candidate_url)
        if page_response is None:
            return None, {"reason": "failed_fetch", "candidate_url": candidate_url}

        soup = BeautifulSoup(page_response.text, "html.parser")

        # 1) iframe/embed/object src/data
        for tag in soup.select("iframe[src], embed[src], object[data]"):
            src = tag.get("src") or tag.get("data")
            if not src:
                continue
            absolute = normalize_pdf_url(urljoin(candidate_url, src))
            if is_pdf_candidate(absolute) or ".pdf" in absolute.lower():
                return absolute, {"resolution_method": "iframe_candidate", "resolved_from": candidate_url}

        # 2) anchors with download attr or obvious download-like labels
        for a in soup.select("a[href]"):
            if a.has_attr("download"):
                href = a.get("href")
                if not href:
                    continue
                absolute = normalize_pdf_url(urljoin(candidate_url, href))
                return absolute, {"resolution_method": "download_attr", "resolved_from": candidate_url}
            label = (a.get_text(" ", strip=True) or "").lower()
            if self._matches_download_keyword(label):
                href = a.get("href")
                if not href:
                    continue
                absolute = normalize_pdf_url(urljoin(candidate_url, href))
                return absolute, {"resolution_method": "download_label", "resolved_from": candidate_url}

        # 3) simple onclick -> url patterns (do not execute JS)
        import re
        for el in soup.select("button, a[onclick]"):
            onclick = el.get("onclick") or el.get("data-href") or el.get("data-url")
            if not onclick:
                continue
            m = re.search(r"['\"](https?://[^'\"]+)['\"]", str(onclick))
            if m:
                absolute = normalize_pdf_url(m.group(1))
                return absolute, {"resolution_method": "onclick_url", "resolved_from": candidate_url}

        # If an intermediate landing page is needed, let the main crawl discover it
        return None, {"reason": "no_static_candidate", "candidate_url": candidate_url}

    def _branch_signature(self, page_url: str) -> str:
        parsed = urlparse(page_url)
        parts = [p for p in (parsed.path or "").split("/") if p]
        return "/" + "/".join(parts[:2]) if parts else "/"

    def _extract_title(self, soup: BeautifulSoup) -> str:
        elem = soup.select_one("title")
        if elem and elem.get_text(strip=True):
            return elem.get_text(strip=True)
        return "Untitled"

    def _discover_recursive(self, url: str):
        """Generator that yields discovery records (dict).

        Emits normalized records; dedup is handled by the caller/writer but
        a local seen set prevents duplicate yields within the same run.
        """
        root = normalize_pdf_url(url)
        root_netloc = urlparse(root).netloc.lower()
        visited: Set[str] = set()
        branch_empty_counts: Dict[str, int] = {}
        queue: Deque[Tuple[str, int]] = deque([(root, 0)])
        local_seen: Set[str] = set()

        while queue and len(visited) < self.max_pages:
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
            page_title = self._extract_title(soup)
            page_date = self._normalize_publication_date(self._extract_pub_date(soup, soup))
            branch = self._branch_signature(current)
            page_new = 0

            anchors = list(soup.select("a[href]"))
            anchors.sort(key=lambda a: 0 if is_wordpress_pdf(urljoin(current, str(a.get("href") or "")) or "") else 1)

            for anchor in anchors:
                href = (anchor.get("href") or "").strip()
                if not href:
                    continue
                absolute = normalize_pdf_url(urljoin(current, href))
                label = (anchor.get_text(" ", strip=True) or "").lower()
                has_keyword = self._matches_download_keyword(absolute) or self._matches_download_keyword(label)
                candidate = is_pdf_candidate(absolute) or has_keyword
                if not candidate:
                    if self.deep_extraction_enabled and has_keyword and depth < self.max_depth:
                        parsed = urlparse(absolute)
                        if parsed.scheme in ("http", "https") and absolute not in visited:
                            queue.append((absolute, depth + 1))
                    continue

                # compute a confidence score based on static heuristics
                score = 0.5
                if absolute.lower().endswith(".pdf"):
                    score = 1.0
                elif anchor.has_attr("download"):
                    score = 0.9
                elif "pdf" in label:
                    score = 0.7

                resolved_pdf_url, resolver_meta = self._resolve_direct_pdf_url(absolute)
                if not resolved_pdf_url:
                    continue

                key = normalize_pdf_url(resolved_pdf_url)
                if not key or key in local_seen:
                    continue
                local_seen.add(key)

                context = {
                    "page_title": page_title,
                    "anchor_text": anchor.get_text(" ", strip=True) or None,
                    "surrounding_text": surrounding_text_for_anchor(anchor, max_chars=300),
                }
                metadata = {
                    "site_name": root_netloc,
                    "discovery_method": "crawl",
                    "extracted_date": page_date,
                    "confidence_score": float(score),
                }
                record = {
                    "pdf_url": key,
                    "parent_page_url": current,
                    "context": context,
                    "metadata": metadata,
                    "resolver_meta": dict(resolver_meta or {}),
                }
                yield record
                page_new += 1

            if page_new == 0:
                branch_empty_counts[branch] = int(branch_empty_counts.get(branch, 0)) + 1
            else:
                branch_empty_counts[branch] = 0

            if branch_empty_counts.get(branch, 0) >= self.branch_prune_threshold:
                continue

            if depth >= self.max_depth:
                continue
            for anchor in anchors:
                href = (anchor.get("href") or "").strip()
                if not href:
                    continue
                absolute = normalize_pdf_url(urljoin(current, href))
                parsed = urlparse(absolute)
                if parsed.scheme not in ("http", "https"):
                    continue
                if parsed.netloc.lower() != root_netloc:
                    continue
                if is_pdf_candidate(absolute):
                    continue
                if absolute in visited:
                    continue
                queue.append((absolute, depth + 1))

    def run_listing(self, listing_url: str, limit: int = 10):
        """Utility method returning in-memory docs from a listing page."""
        docs = []
        domain = urlparse(listing_url).netloc.lower()
        for idx in range(max(1, int(limit))):
            markdown = (
                "# Listing Discovery Placeholder\n\n"
                f"- listing_url: {listing_url}\n"
                f"- domain: {domain}\n"
                f"- item_index: {idx + 1}\n"
            )
            docs.append((
                self._build_document(
                    url=listing_url,
                    title="Listing Discovery",
                    markdown=markdown,
                    metadata={"source_type": "news_pdf_discovery_listing"},
                ),
                None,
            ))
        return docs

    def _extract_pub_date(self, node: Optional[BeautifulSoup], root: Optional[BeautifulSoup] = None) -> Optional[str]:
        """Best-effort extraction of publication date from common meta tags or time tags.

        The function first searches inside `node` (article fragment), then falls
        back to `root` (full soup) to locate <meta> tags in the document head.
        """
        # Discovery-only: minimal best-effort date extraction. If not found, return None.
        try:
            if node is not None:
                meta = node.select_one("meta[name=date], meta[property='article:published_time']")
                if meta and meta.has_attr('content'):
                    return (meta.get('content') or '').strip()
                time_el = node.select_one("time[datetime]")
                if time_el and time_el.has_attr('datetime'):
                    return (time_el.get('datetime') or '').strip()
            if root is not None:
                meta = root.select_one("meta[name=date], meta[property='article:published_time']")
                if meta and meta.has_attr('content'):
                    return (meta.get('content') or '').strip()
        except Exception:
            return None
        return None
