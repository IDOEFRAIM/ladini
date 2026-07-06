from __future__ import annotations
"""
Base scraper contract (V2)

This module defines `BaseScraper`, the abstract class all V2 scrapers
must inherit from. It centralizes:
- HTTP session and headers management (User-Agent rotation),
- polite fetching logic (throttling, robots.txt),
- canonical construction of `RawDocument` and `ScraperLog`.

Contract: implement `scrape(url) -> (RawDocument|None, meta_dict)` and use `run(url)` to
return `(RawDocument, ScraperLog)`.
"""

import time
import traceback
import random
import re
import ast
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from agriconnect.core.schemas import RawDocument, ScraperLog
from agriconnect.core.scraper_utils import (
    HttpPolicy,
    apply_throttle,
    can_fetch_url,
    content_density,
    content_sha256,
    create_session,
    normalize_text,
    get_random_user_agent,
)
from .message_models import ScraperQueueMessage
from .sqs_provider import SQSProvider


logger = logging.getLogger(__name__)

DEFAULT_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Referer": "https://google.com",
}

class BaseScraper(ABC):
    """Base contract for all scraper bricks."""

    name: str = "base"
    version: str = "2.0.0"

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        network_params = self.config.get("network_params") if isinstance(self.config.get("network_params"), dict) else {}
        processing_cfg = self.config.get("processing") if isinstance(self.config.get("processing"), dict) else {}
        self.network_params = dict(network_params or {})
        self.processing = dict(processing_cfg or {})
        self.policy = HttpPolicy(
            timeout=int(self.config.get("timeout", self.network_params.get("timeout", 20))),
            min_delay_s=float(self.config.get("min_delay_s", self.network_params.get("min_delay_s", 0.5))),
            max_delay_s=float(self.config.get("max_delay_s", self.network_params.get("max_delay_s", 1.6))),
            max_attempts=int(self.config.get("max_attempts", self.network_params.get("max_attempts", 4))),
            respect_robots=bool(self.config.get("respect_robots", self.network_params.get("respect_robots", True))),
            user_agents=tuple(self.config.get("user_agents") or self.network_params.get("user_agents") or []),
        )
        agent = self.config.get("user_agent") or self.network_params.get("user_agent")
        self.session = create_session(user_agent=agent)
        self.session.headers.update(DEFAULT_BROWSER_HEADERS)

        # Optional request-level headers/cookies declared in YAML.
        extra_headers = self.network_params.get("headers")
        if isinstance(extra_headers, dict):
            self.session.headers.update({str(k): str(v) for k, v in extra_headers.items() if v is not None})
        extra_cookies = self.network_params.get("cookies")
        if isinstance(extra_cookies, dict):
            self.session.cookies.update({str(k): str(v) for k, v in extra_cookies.items() if v is not None})

        # Centralized circuit-breaker for domain failures (moved from scrapers)
        self.circuit_threshold = int(self.config.get("circuit_threshold", self.network_params.get("circuit_threshold", 3)))
        self.circuit_quarantine_minutes = int(self.config.get("circuit_quarantine_minutes", self.network_params.get("circuit_quarantine_minutes", 30)))
        self._domain_failures: Dict[str, int] = {}
        self._domain_quarantine_until: Dict[str, datetime] = {}
        self.sqs_provider: Optional[SQSProvider] = None

        sqs_cfg = self.config.get("sqs") if isinstance(self.config.get("sqs"), dict) else {}
        queue_url = str(sqs_cfg.get("queue_url") or "").strip()
        region_name = str(sqs_cfg.get("region") or "").strip()
        if queue_url and region_name:
            self.sqs_provider = SQSProvider(queue_url=queue_url, region_name=region_name)

    def set_sqs_provider(self, provider: SQSProvider) -> None:
        self.sqs_provider = provider

    def enqueue_pdf_url(self, *, url: str, source_id: str, partner_metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Publish a PDF discovery event to SQS (cloud-native flow)."""
        if self.sqs_provider is None:
            raise RuntimeError("SQS provider is not configured")

        message = ScraperQueueMessage(
            url=url,
            source_id=source_id,
            partner_metadata=partner_metadata or {},
        )
        return self.sqs_provider.send_scraper_message(message)

    def _before_fetch(self, url: str) -> None:
        apply_throttle(self.policy)
        # Meteo Burkina can reset connections aggressively; use a human-like pause.
        if "meteoburkina.bf" in (url or "").lower():
            time.sleep(random.uniform(1.0, 3.0))
        # Rotate User-Agent per-request to evade simple bot-blocking rules
        ua = None
        try:
            agents = tuple(self.policy.user_agents or ())
            if agents:
                ua = random.choice(agents)
            else:
                ua = DEFAULT_BROWSER_HEADERS["User-Agent"]
        except Exception:
            ua = DEFAULT_BROWSER_HEADERS["User-Agent"]

        # Update session header for this request
        self.session.headers["User-Agent"] = ua
        self.session.headers.setdefault("Accept-Language", DEFAULT_BROWSER_HEADERS["Accept-Language"])
        self.session.headers.setdefault("Referer", DEFAULT_BROWSER_HEADERS["Referer"])

        if self.policy.respect_robots and not can_fetch_url(url, ua):
            raise PermissionError(f"robots.txt forbids scraping: {url}")

    @staticmethod
    def _is_connection_reset_error(exc: Exception) -> bool:
        text = str(exc).lower()
        if "connection reset" in text or "winerror 10054" in text or "errno 104" in text:
            return True
        current = exc
        for _ in range(4):
            if current is None:
                break
            nested = str(current).lower()
            if "connection reset" in nested or "winerror 10054" in nested or "errno 104" in nested:
                return True
            current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        return False

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
        try:
            if status in (403, 429):
                failures = int(self._domain_failures.get(domain, 0)) + 1
                self._domain_failures[domain] = failures
                if failures >= self.circuit_threshold:
                    self._domain_quarantine_until[domain] = datetime.now(timezone.utc) + timedelta(minutes=self.circuit_quarantine_minutes)
            elif status < 400:
                self._domain_failures.pop(domain, None)
        except Exception:
            # Be defensive: do not let monitoring logic break fetch flow
            return

    def _request(self, url: str) -> requests.Response:
        max_attempts = max(1, int(self.policy.max_attempts or 1))
        backoff_initial_s = float(self.network_params.get("connection_reset_backoff_initial_s", 1.0))
        backoff_multiplier = float(self.network_params.get("connection_reset_backoff_multiplier", 2.0))
        backoff_max_s = float(self.network_params.get("connection_reset_backoff_max_s", 20.0))
        jitter_s = float(self.network_params.get("connection_reset_backoff_jitter_s", 0.25))

        for attempt in range(1, max_attempts + 1):
            try:
                self._before_fetch(url)
                response = self.session.get(url, timeout=self.policy.timeout)
                response.raise_for_status()
                return response
            except requests.exceptions.ConnectionError as exc:
                is_reset = self._is_connection_reset_error(exc)
                if (not is_reset) or attempt >= max_attempts:
                    raise
                delay = min(backoff_max_s, backoff_initial_s * (backoff_multiplier ** (attempt - 1)))
                if jitter_s > 0:
                    delay += random.uniform(0, jitter_s)
                logger.warning(
                    "Connection reset while fetching %s (attempt %d/%d), retry in %.2fs",
                    url,
                    attempt,
                    max_attempts,
                    delay,
                )
                time.sleep(delay)

        raise RuntimeError(f"Request failed after retries: {url}")

    def _clean_soup(self, soup: BeautifulSoup) -> BeautifulSoup:
        """Drop high-noise nodes and keep only useful attributes for downstream markdown."""
        # Broad structural noise
        noisy_selectors = (
            "nav, footer, header, aside, script, style, noscript, iframe, \
            [role=navigation], [role=banner], [role=dialog], [aria-hidden=true], \
            .ads, .advertisement, .advert, .ad, .social-share, .share, .sidebar, \
            .cookie, .cookie-banner, .cookie-consent, .newsletter, .subscribe, \
            .subscribe-box, .related-posts, .related-articles, .promo, .promo-box, \
            .breadcrumbs, .breadcrumb, .site-header, .site-footer, .topbar, .navbar, .modal, .popup"
        )
        for noisy in soup.select(noisy_selectors):
            try:
                noisy.decompose()
            except Exception:
                pass

        allowed_attrs = {"href", "src", "alt", "title", "datetime", "content", "name", "property", "srcset", "role", "aria-label", "aria-hidden"}
        for tag in soup.find_all(True):
            if not tag.attrs:
                continue
            tag.attrs = {k: v for k, v in tag.attrs.items() if k in allowed_attrs}
        # Heuristic: drop blocks that are mostly navigation/links with little textual content
        for block in soup.find_all(["div", "section", "nav", "aside", "header", "footer"]):
            try:
                text = (block.get_text(" ", strip=True) or "")
                link_count = len(block.select("a[href]"))
                if link_count >= 6 and len(text) < 120:
                    block.decompose()
            except Exception:
                continue

        return soup

    def _absolutize_links(self, soup: BeautifulSoup, base_url: str) -> BeautifulSoup:
        for anchor in soup.select("a[href]"):
            href = (anchor.get("href") or "").strip()
            if href:
                anchor["href"] = urljoin(base_url, href)
        for img in soup.select("img[src]"):
            src = (img.get("src") or "").strip()
            if src:
                img["src"] = urljoin(base_url, src)
        return soup

    def _filter_navigation_links(self, soup: BeautifulSoup) -> BeautifulSoup:
        noise_terms = (
            "retour en haut",
            "back to top",
            "share",
            "partager",
            "whatsapp",
            "facebook",
            "twitter",
            "linkedin",
        )
        keep_exts = (".pdf", ".xlsx", ".xls", ".csv", ".doc", ".docx", ".zip")
        for anchor in soup.select("a[href]"):
            href = (anchor.get("href") or "").lower()
            text = (anchor.get_text(" ", strip=True) or "").lower()
            if any(ext in href for ext in keep_exts):
                continue
            if any(term in text for term in noise_terms):
                anchor.decompose()
        return soup

    def _prepare_markdown_html(self, html: str, base_url: str, preferred_selectors: Optional[object] = None) -> str:
        """Prepare a cleaned HTML fragment for markdown extraction.

        `preferred_selectors` may be provided as a comma-separated string or
        as a YAML sequence (list). We tolerate either and sanitize trailing
        commas so that soupsieve does not raise a SelectorSyntaxError.
        """
        soup = BeautifulSoup(html or "", "html.parser")
        self._clean_soup(soup)
        self._absolutize_links(soup, base_url)
        self._filter_navigation_links(soup)

        effective_selectors = preferred_selectors
        if effective_selectors is None:
            selectors_cfg = self.config.get("selectors") if isinstance(self.config.get("selectors"), dict) else {}
            if isinstance(selectors_cfg, dict):
                effective_selectors = selectors_cfg.get("body") or selectors_cfg.get("content")

        if effective_selectors:
            # Accept lists (from YAML sequences) or strings (comma-separated)
            try:
                if isinstance(effective_selectors, (list, tuple)):
                    for sel in effective_selectors:
                        sel_str = (str(sel) or "").strip()
                        if not sel_str:
                            continue
                        node = soup.select_one(sel_str)
                        if node is not None:
                            return str(node)
                else:
                    # Normalize string: remove accidental trailing commas and
                    # collapse multiple commas/whitespace into a well-formed
                    # selector list for soupsieve.
                    sel_raw = str(effective_selectors).strip()

                    # Some callers may pass a stringified Python list, e.g.
                    # "['main', 'article', '#content']". Parse and iterate it.
                    if sel_raw.startswith("[") and sel_raw.endswith("]"):
                        try:
                            parsed = ast.literal_eval(sel_raw)
                            if isinstance(parsed, (list, tuple)):
                                for sel in parsed:
                                    sel_item = (str(sel) or "").strip()
                                    if not sel_item:
                                        continue
                                    node = soup.select_one(sel_item)
                                    if node is not None:
                                        return str(node)
                        except Exception:
                            pass

                    # Remove trailing commas
                    sel_clean = re.sub(r",+\s*$", "", sel_raw)
                    # Collapse repeated commas/spaces
                    sel_clean = re.sub(r"\s*,\s*", ", ", sel_clean)
                    if sel_clean:
                        node = soup.select_one(sel_clean)
                        if node is not None:
                            return str(node)
            except Exception:
                # If selector parsing fails, fall back to default extraction.
                logger.debug("preferred_selectors parsing failed: %r", effective_selectors)

        node = soup.select_one("main, article, #content, .post-content, .article-content")
        return str(node) if node is not None else str(soup)

    @staticmethod
    def _contains_waf_or_block(text: str) -> bool:
        low = (text or "").lower()
        # Keep detection strict to avoid false positives on legitimate pages
        # that reference vendor assets (e.g. "cloudflare" in script URLs).
        strict_signatures = (
            "please enable js",
            "please enable javascript",
            "access denied",
            "checking your browser",
            "attention required",
            "verify you are human",
            "captcha",
            "cf-chl-",
            "/cdn-cgi/challenge",
            "challenge-platform",
            "why do i have to complete a captcha",
        )
        if any(sig in low for sig in strict_signatures):
            return True
        # Cloudflare alone is not enough; require a challenge/deny context.
        if "cloudflare" in low and ("challenge" in low or "access denied" in low or "captcha" in low):
            return True
        return False

    def _assess_extraction_quality(self, markdown: str, raw_html: str) -> Dict[str, bool]:
        md_len = len((markdown or "").strip())
        html_len = len((raw_html or "").encode("utf-8", errors="ignore"))
        blocked = self._contains_waf_or_block(raw_html or "") or self._contains_waf_or_block(markdown or "")
        partial = md_len < 100 and html_len >= 10 * 1024
        return {
            "blocked_by_waf": blocked,
            "partial_extraction": partial,
        }

    @staticmethod
    def _extract_language_code(soup: Optional[BeautifulSoup]) -> Optional[str]:
        if soup is None:
            return None
        html_tag = soup.find("html")
        if html_tag is not None:
            lang = (html_tag.get("lang") or "").strip().lower()
            if lang:
                return lang.split("-")[0][:2]
        return None

    @staticmethod
    def _source_domain(url: str) -> str:
        return (urlparse(url).netloc or "").lower()

    @staticmethod
    def _normalize_publication_date(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        raw = value.strip()
        m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
        if m:
            return m.group(1)
        for fmt in ("%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(raw[:10], fmt).strftime("%Y-%m-%d")
            except Exception:
                continue
        return None

    def _build_document(self, *, url: str, title: str, markdown: str, metadata: Optional[Dict] = None, language: Optional[str] = None) -> RawDocument:
        clean_md = normalize_text(self._apply_processing_rules(markdown))
        return RawDocument(
            id=content_sha256(clean_md),
            url=url,
            title=(title or "Untitled").strip(),
            content_markdown=clean_md,
            metadata=metadata or {},
            language=language,
        )

    def _apply_processing_rules(self, markdown: str) -> str:
        """Apply optional post-scraping cleanup rules from YAML processing config."""
        text = markdown or ""
        rules = self.processing or {}

        cleanup_patterns = rules.get("cleanup_regex")
        if isinstance(cleanup_patterns, list):
            for pattern in cleanup_patterns:
                try:
                    text = re.sub(str(pattern), "", text, flags=re.MULTILINE | re.IGNORECASE)
                except Exception:
                    continue

        replacements = rules.get("replace")
        if isinstance(replacements, list):
            for item in replacements:
                if not isinstance(item, dict):
                    continue
                pattern = item.get("pattern")
                repl = item.get("with", "")
                if not pattern:
                    continue
                try:
                    text = re.sub(str(pattern), str(repl), text, flags=re.MULTILINE)
                except Exception:
                    continue

        return text

    def run(self, url: str) -> Tuple[Optional[RawDocument], ScraperLog]:
        started = time.time()
        try:
            doc, meta = self.scrape(url)
            log = ScraperLog(
                scraper_name=self.name,
                version=self.version,
                http_status=meta.get("http_status"),
                duration_ms=int((time.time() - started) * 1000),
                bytes_downloaded=int(meta.get("bytes_downloaded", 0)),
                content_density=content_density(doc.content_markdown if doc else "", str(meta.get("raw_payload", ""))),
                success=doc is not None,
            )
            return doc, log

        except Exception:
            log = ScraperLog(
                scraper_name=self.name,
                version=self.version,
                http_status=None,
                duration_ms=int((time.time() - started) * 1000),
                bytes_downloaded=0,
                content_density=0.0,
                success=False,
                error_trace=traceback.format_exc(limit=10),
            )
            return None, log

    @abstractmethod
    def scrape(self, url: str) -> Tuple[Optional[RawDocument], Dict]:
        """Returns (RawDocument|None, meta_dict for logging)."""