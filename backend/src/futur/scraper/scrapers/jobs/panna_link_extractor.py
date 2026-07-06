from __future__ import annotations

"""
Panna.org Link Extractor (site-specific discovery)

Lightweight, site-specific extractor that crawls Panna.org sitemaps/pages to
collect PDF candidate URLs and emits discovery NDJSON via `DiscoveryWriter`.
This script is discovery-only and does NOT perform binary validation or
downloading; ingestion is responsible for fetching/validating PDFs.
"""

import json
import os
import random
import re
import sys
import time
from collections import deque
from datetime import datetime
from typing import Deque, Dict, Iterable, List, Optional, Set
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from lxml import etree

from .pdf_discovery import DiscoveryWriter

# Config
ROOT = "https://www.panna.org/"
SITEMAP_INDEXS = [
    "https://www.panna.org/sitemap_index.xml",
    "https://www.panna.org/page-sitemap.xml",
]
OUT_JSON = os.path.join(os.getcwd(), "pdf_queue.json")
NDJSON_OUT = os.path.join(os.getcwd(), "data", "pdf_discovery", "panna_discovery.ndjson")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
PDF_REGEX = re.compile(r"https?://[^\s\"'<>]+wp-content/uploads/[^\s\"'<>]+\.pdf", re.IGNORECASE)


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url
    parsed = urlparse(url)
    if not parsed.scheme:
        # assume https
        parsed = parsed._replace(scheme="https")
    # remove query and fragment
    parsed = parsed._replace(query="", fragment="")
    # normalize netloc default ports
    netloc = parsed.netloc.replace(":80", "").replace(":443", "")
    parsed = parsed._replace(netloc=netloc)
    return urlunparse(parsed)


def build_session(timeout: int = 30) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    })
    s.request_timeout = timeout  # type: ignore[attr-defined]
    return s


def polite_sleep(min_s: float = 0.5, max_s: float = 1.5) -> None:
    time.sleep(random.uniform(min_s, max_s))


def parse_sitemap(xml_content: bytes) -> List[str]:
    out: List[str] = []
    try:
        root = etree.fromstring(xml_content)
    except Exception:
        return out
    locs = root.xpath("//*[local-name()='loc']")
    for n in locs:
        if n is not None and n.text:
            out.append(n.text.strip())
    return out


def fetch_text(session: requests.Session, url: str, timeout: int = 30) -> Optional[str]:
    try:
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException:
        return None


def discover_pages_from_sitemaps(session: requests.Session, min_delay: float, max_delay: float) -> Set[str]:
    discovered_sitemaps: Set[str] = set()
    page_urls: Set[str] = set()

    queue: Deque[str] = deque(normalize_url(u) for u in SITEMAP_INDEXS)
    while queue:
        sm = queue.popleft()
        if not sm or sm in discovered_sitemaps:
            continue
        discovered_sitemaps.add(sm)
        try:
            resp = session.get(sm, timeout=30)
            resp.raise_for_status()
            content = resp.content
        except requests.RequestException:
            polite_sleep(min_delay, max_delay)
            continue
        polite_sleep(min_delay, max_delay)
        for loc in parse_sitemap(content):
            loc_n = normalize_url(loc)
            # follow nested sitemaps
            if loc_n.endswith('.xml') or 'sitemap' in loc_n.lower():
                if loc_n not in discovered_sitemaps:
                    queue.append(loc_n)
                continue
            if loc_n:
                page_urls.add(loc_n)
    return page_urls


def extract_pdfs_from_html(page_url: str, html: str) -> Set[str]:
    found: Set[str] = set()
    if not html:
        return found
    # regex find absolute PDF urls
    for m in PDF_REGEX.findall(html):
        found.add(normalize_url(m))
    # also parse anchors
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select('a[href]'):
        href = (a.get('href') or '').strip()
        if not href:
            continue
        full = normalize_url(urljoin(page_url, href))
        low = full.lower()
        if 'wp-content/uploads/' in low and low.endswith('.pdf'):
            found.add(full)
    return found


def crawl_recursive_for_pdfs(
    session: requests.Session,
    start_urls: Iterable[str],
    depth: int = 5,
    min_delay: float = 0.5,
    max_delay: float = 1.5,
    page_limit: int = 10000,
    autosave_callback=None,
    autosave_every: int = 20,
) -> Dict[str, str]:
    root_netloc = urlparse(ROOT).netloc.lower()
    queue: Deque[tuple[str, int]] = deque((u, 0) for u in start_urls)
    visited: Set[str] = set()
    found: Dict[str, str] = {}
    pages_analyzed = 0
    consecutive_no_new = 0
    noise_patterns = ['/feed/', 'replytocom=', '/xmlrpc', '?share=', '/comments/']
    new_since_save = 0

    while queue and pages_analyzed < page_limit:
        url, cur_depth = queue.popleft()
        if url in visited:
            continue
        if cur_depth > depth:
            continue
        visited.add(url)

        html = fetch_text(session, url)
        polite_sleep(min_delay, max_delay)
        pages_analyzed += 1

        found_before = len(found)
        if html:
            for p in extract_pdfs_from_html(url, html):
                if p not in found:
                    found.setdefault(p, url)
                    new_since_save += 1

            soup = BeautifulSoup(html, 'html.parser')
            for a in soup.select('a[href]'):
                href = (a.get('href') or '').strip()
                if not href:
                    continue
                nxt = normalize_url(urljoin(url, href))
                if not nxt.startswith('http'):
                    continue
                if urlparse(nxt).netloc.lower() != root_netloc:
                    continue
                low = nxt.lower()
                # skip noisy URLs
                if any(pat in low for pat in noise_patterns):
                    continue
                if nxt in visited:
                    continue
                queue.append((nxt, cur_depth + 1))

        # progress log
        if pages_analyzed % 50 == 0:
            print(f"Pages analysées : {pages_analyzed} | PDF trouvés : {len(found)}")

        # autosave when threshold reached
        if autosave_callback and new_since_save >= autosave_every:
            try:
                autosave_callback(set(found.keys()))
            except Exception:
                pass
            new_since_save = 0

        # early stopping: if many pages without new PDFs
        if len(found) == found_before:
            consecutive_no_new += 1
        else:
            consecutive_no_new = 0

        if consecutive_no_new >= 500:
            print(f"[STOP] No new PDFs after {consecutive_no_new} consecutive pages — early stopping")
            break

    print(f"Pages analysées : {pages_analyzed} | PDF trouvés : {len(found)}")
    # final autosave
    if autosave_callback and new_since_save > 0:
        try:
            autosave_callback(set(found.keys()))
        except Exception:
            pass
    return found


def main() -> int:
    session = build_session()
    writer = DiscoveryWriter(NDJSON_OUT)
    # prepare autosave helper
    def save_queue(links_set: Set[str]):
        final_links = sorted({u.rstrip('/') for u in links_set})
        payload = {
            "total_links": len(final_links),
            "last_updated": datetime.utcnow().strftime('%Y-%m-%d'),
            "links": final_links,
        }
        try:
            with open(OUT_JSON, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            print(f"[AUTOSAVE] wrote {len(final_links)} links to {OUT_JSON}")
        except Exception as exc:
            print(f"[WARN] autosave failed: {exc}")
        # also append to NDJSON discovery writer (best-effort)
        try:
            for u in final_links:
                try:
                    writer.write({"pdf_url": u, "source": "panna_autosave"})
                except Exception:
                    pass
        except Exception:
            pass

    # Phase 1: sitemaps
    print("[Phase 1] Exploration des sitemaps...")
    pages = discover_pages_from_sitemaps(session, 0.5, 1.5)
    print(f"[Phase 1] pages découvertes via sitemap: {len(pages)}")

    # Phase 2: recursive crawl starting from root + discovered pages
    seeds = set(pages)
    seeds.add(normalize_url(ROOT))

    print("[Phase 2] Crawl récursif (profondeur=5)...")
    try:
        found_from_crawl = crawl_recursive_for_pdfs(
            session,
            seeds,
            depth=5,
            min_delay=0.5,
            max_delay=1.5,
            autosave_callback=save_queue,
            autosave_every=20,
        )
    except KeyboardInterrupt:
        print("[STOP] Interrupted by user during crawl — saving current queue...")
        # try to salvage whatever was found by reading any partial saved file
        # we will rely on autosave callback having saved recent state; continue to finalize
        found_from_crawl = {}

    # Phase 3: aggregate + normalize + dedupe
    all_pdfs = set(found_from_crawl.keys())

    # final normalization (remove trailing slashes)
    final_links = sorted({u.rstrip('/') for u in all_pdfs})
    # write legacy JSON summary
    payload = {
        "total_links": len(final_links),
        "last_updated": datetime.utcnow().strftime('%Y-%m-%d'),
        "links": final_links,
    }
    try:
        with open(OUT_JSON, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[ERROR] final save failed: {exc}")

    # write NDJSON discovery records with parent context
    try:
        for link in final_links:
            parent = found_from_crawl.get(link)
            rec = {"pdf_url": link, "source": "panna", "parent_page": parent}
            try:
                writer.write(rec)
            except Exception:
                pass
    except Exception:
        print("[WARN] failed writing NDJSON discovery records")

    print(f"[DONE] Ecriture de {OUT_JSON} ({payload['total_links']} liens) and NDJSON {NDJSON_OUT}")
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\n[STOP] Interrupted')
        raise SystemExit(130)
