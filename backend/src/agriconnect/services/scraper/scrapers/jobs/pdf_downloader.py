from __future__ import annotations

import io
import logging
import os
import random
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
import multiprocessing
import traceback

from agriconnect.core.schemas import RawDocument
from agriconnect.core.scraper_utils import content_sha256, normalize_text

from ..shared.base_scraper import BaseScraper

logger = logging.getLogger(__name__)


def _playwright_worker(q, url, timeout_s):
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=max(1000, int(timeout_s) * 1000))
            try:
                page.wait_for_timeout(500)
            except Exception:
                pass
            html = page.content()
            try:
                page.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
        q.put(("ok", html))
    except Exception:
        q.put(("err", traceback.format_exc()))


def _ocr_worker(q, payload, max_pages):
    try:
        from pdf2image import convert_from_bytes  # type: ignore
        import pytesseract  # type: ignore
        images = convert_from_bytes(payload, first_page=1, last_page=min(max_pages, 10))
        parts = []
        for img in images:
            try:
                txt = pytesseract.image_to_string(img)
            except Exception:
                txt = ""
            parts.append(txt or "")
        text = "\n\n".join(p for p in parts if p)
        q.put(("ok", (text, {"total_pages": len(images), "tables_count": 0, "extraction_engine": "ocr"})))
    except Exception:
        q.put(("err", traceback.format_exc()))


@dataclass
class PdfTarget:
    source_url: str
    pdf_url: str
    title_hint: str
    date_publication: Optional[str] = None


class PdfExtractor:
    """PDF extraction engine: table-aware text extraction + fallback path."""

    def __init__(
        self,
        *,
        max_pages: int = 60,
        enable_table_extraction: bool = True,
        fallback_order: Optional[List[str]] = None,
    ) -> None:
        self.max_pages = max_pages
        self.enable_table_extraction = enable_table_extraction
        self.fallback_order = fallback_order or ["pdfplumber", "pymupdf", "pypdf"]

    @staticmethod
    def _normalize_token(value: str) -> str:
        txt = (value or "").lower()
        txt = unicodedata.normalize("NFKD", txt)
        txt = "".join(ch for ch in txt if not unicodedata.combining(ch))
        return re.sub(r"\s+", " ", txt).strip()

    @staticmethod
    def _cell_text(cell: Any) -> str:
        return normalize_text(str(cell or "")).strip()

    @staticmethod
    def _rows_to_markdown(rows: List[List[Any]]) -> str:
        if not rows:
            return ""

        cleaned = [[PdfExtractor._cell_text(c) for c in row] for row in rows]
        width = max((len(r) for r in cleaned), default=0)
        if width <= 0:
            return ""

        header = cleaned[0] + [""] * (width - len(cleaned[0]))
        if all(not h for h in header):
            header = [f"Col {i+1}" for i in range(width)]

        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(["---"] * width) + " |",
        ]
        for row in cleaned[1:]:
            full = row + [""] * (width - len(row))
            lines.append("| " + " | ".join(full) + " |")
        return "\n".join(lines)

    @staticmethod
    def _line_is_page_number(line: str) -> bool:
        return bool(
            re.match(
                r"^\s*(page\s*\d+(\s*(/|sur)\s*\d+)?|\d+\s*/\s*\d+|\d{1,3})\s*$",
                line or "",
                re.I,
            )
        )

    @staticmethod
    def _line_cleanup(line: str) -> str:
        out = re.sub(r"<[^>]+>", " ", line or "")
        out = re.sub(r"\s+", " ", out).strip()
        return out

    @staticmethod
    def _is_in_any_bbox(word: Dict[str, Any], boxes: List[Tuple[float, float, float, float]]) -> bool:
        x0 = float(word.get("x0", 0.0))
        x1 = float(word.get("x1", 0.0))
        top = float(word.get("top", 0.0))
        bottom = float(word.get("bottom", 0.0))
        cx = (x0 + x1) / 2.0
        cy = (top + bottom) / 2.0
        for bx0, btop, bx1, bbottom in boxes:
            if bx0 <= cx <= bx1 and btop <= cy <= bbottom:
                return True
        return False

    @staticmethod
    def _group_words_to_lines(words: List[Dict[str, Any]], y_tol: float = 3.0) -> List[Tuple[float, str]]:
        if not words:
            return []

        words_sorted = sorted(words, key=lambda w: (float(w.get("top", 0.0)), float(w.get("x0", 0.0))))
        groups: List[List[Dict[str, Any]]] = []
        current: List[Dict[str, Any]] = []
        current_y: Optional[float] = None

        for w in words_sorted:
            y = float(w.get("top", 0.0))
            if current_y is None or abs(y - current_y) <= y_tol:
                current.append(w)
                current_y = y if current_y is None else (current_y + y) / 2.0
            else:
                groups.append(current)
                current = [w]
                current_y = y
        if current:
            groups.append(current)

        lines: List[Tuple[float, str]] = []
        for g in groups:
            g_sorted = sorted(g, key=lambda w: float(w.get("x0", 0.0)))
            txt = normalize_text(" ".join(str(w.get("text", "")) for w in g_sorted))
            if txt:
                y = float(g_sorted[0].get("top", 0.0))
                lines.append((y, txt))
        return lines

    @staticmethod
    def _table_signature(rows: List[List[Any]]) -> Tuple[int, Tuple[str, ...]]:
        if not rows:
            return (0, tuple())
        ncols = max((len(r) for r in rows), default=0)
        header = tuple(PdfExtractor._cell_text(c).lower() for c in (rows[0] if rows else []))
        return (ncols, header)

    def _stitch_tables(self, blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not blocks:
            return blocks

        stitched: List[Dict[str, Any]] = []
        for block in blocks:
            if block.get("kind") != "table":
                stitched.append(block)
                continue

            if not stitched or stitched[-1].get("kind") != "table":
                stitched.append(block)
                continue

            prev = stitched[-1]
            sig_prev = self._table_signature(prev.get("rows", []))
            sig_cur = self._table_signature(block.get("rows", []))
            if sig_prev == sig_cur and sig_prev[0] > 0:
                prev_rows = prev.get("rows", [])
                cur_rows = block.get("rows", [])
                if cur_rows:
                    cur_header = [self._cell_text(c) for c in cur_rows[0]]
                    prev_header = [self._cell_text(c) for c in (prev_rows[0] if prev_rows else [])]
                    if prev_header and cur_header and prev_header == cur_header:
                        cur_rows = cur_rows[1:]
                prev["rows"] = prev_rows + cur_rows
                prev["markdown"] = self._rows_to_markdown(prev["rows"])
            else:
                stitched.append(block)

        return stitched

    def _extract_with_pdfplumber(self, payload: bytes) -> Tuple[str, Dict[str, Any]]:
        import pdfplumber  # type: ignore

        page_data: List[Dict[str, Any]] = []

        with pdfplumber.open(io.BytesIO(payload)) as pdf:
            for i, page in enumerate(pdf.pages[: self.max_pages], start=1):
                tables = page.find_tables() or []
                table_entries: List[Dict[str, Any]] = []
                table_boxes: List[Tuple[float, float, float, float]] = []
                for t in tables:
                    rows = t.extract() or []
                    if not rows:
                        continue
                    bbox = t.bbox if getattr(t, "bbox", None) else None
                    if bbox:
                        bx0, btop, bx1, bbottom = [float(v) for v in bbox]
                        table_boxes.append((bx0, btop, bx1, bbottom))
                        y = btop
                    else:
                        y = float(len(table_entries) * 1000)
                    table_entries.append(
                        {
                            "kind": "table",
                            "page": i,
                            "y": y,
                            "rows": rows,
                            "markdown": self._rows_to_markdown(rows),
                        }
                    )

                words = page.extract_words(keep_blank_chars=False, x_tolerance=2, y_tolerance=3) or []
                words_outside_tables = [w for w in words if not self._is_in_any_bbox(w, table_boxes)]
                text_lines = self._group_words_to_lines(words_outside_tables)

                text_entries: List[Dict[str, Any]] = []
                total = len(text_lines)
                for idx, (y, line) in enumerate(text_lines):
                    text_entries.append(
                        {
                            "kind": "text",
                            "page": i,
                            "y": y,
                            "text": line,
                            "edge": idx < 3 or idx >= max(total - 3, 0),
                        }
                    )

                page_data.append(
                    {
                        "page": i,
                        "text_entries": text_entries,
                        "table_entries": table_entries,
                    }
                )

        # Detect repeated headers/footers from edge lines across pages.
        edge_counts: Dict[str, int] = {}
        for pdata in page_data:
            for e in pdata["text_entries"]:
                if not e.get("edge"):
                    continue
                token = self._normalize_token(e.get("text", ""))
                if not token:
                    continue
                edge_counts[token] = edge_counts.get(token, 0) + 1
        repeated_edge = {k for k, v in edge_counts.items() if v >= 2}

        raw_blocks: List[Dict[str, Any]] = []
        for pdata in page_data:
            merged = list(pdata["text_entries"]) + list(pdata["table_entries"])
            merged.sort(key=lambda b: float(b.get("y", 0.0)))
            for entry in merged:
                if entry.get("kind") == "text":
                    text = self._line_cleanup(entry.get("text", ""))
                    token = self._normalize_token(text)
                    if not text:
                        continue
                    if self._line_is_page_number(text):
                        continue
                    if token in repeated_edge and len(token) <= 120:
                        continue
                    raw_blocks.append({"kind": "text", "text": text})
                elif entry.get("kind") == "table":
                    md = entry.get("markdown", "")
                    if not md:
                        continue
                    raw_blocks.append({"kind": "table", "rows": entry.get("rows", []), "markdown": md})

        stitched = self._stitch_tables(raw_blocks)

        # Collapse adjacent text blocks.
        final_blocks: List[Dict[str, Any]] = []
        for b in stitched:
            if b.get("kind") == "text":
                if final_blocks and final_blocks[-1].get("kind") == "text":
                    final_blocks[-1]["text"] = normalize_text(final_blocks[-1]["text"] + "\n" + b["text"])
                else:
                    final_blocks.append({"kind": "text", "text": b["text"]})
            else:
                final_blocks.append(b)

        parts: List[str] = []
        for b in final_blocks:
            if b.get("kind") == "text":
                parts.append(b.get("text", ""))
            else:
                parts.append(b.get("markdown", ""))

        markdown = "\n\n".join(p for p in parts if p).strip()
        return markdown, {
            "total_pages": len(page_data),
            "tables_count": len([b for b in final_blocks if b.get("kind") == "table"]),
            "extraction_engine": "pdfplumber",
        }

    def _extract_with_pymupdf(self, payload: bytes) -> Tuple[str, Dict[str, Any]]:
        import fitz  # type: ignore

        pages: List[str] = []
        with fitz.open(stream=payload, filetype="pdf") as doc:
            for i, page in enumerate(doc, start=1):
                if i > self.max_pages:
                    break
                text = page.get_text("text") or ""
                text = re.sub(r"<[^>]+>", " ", text)
                lines = [self._line_cleanup(l) for l in text.splitlines()]
                cleaned = [l for l in lines if l and not self._line_is_page_number(l)]
                if cleaned:
                    pages.append("\n".join(cleaned))

        md = "\n\n".join(pages).strip()
        return md, {
            "total_pages": len(pages),
            "tables_count": 0,
            "extraction_engine": "pymupdf",
        }

    def _extract_with_pypdf(self, payload: bytes) -> Tuple[str, Dict[str, Any]]:
        reader = PdfReader(io.BytesIO(payload))
        pages: List[str] = []
        for i, page in enumerate(reader.pages[: self.max_pages], start=1):
            text = page.extract_text() or ""
            text = re.sub(r"<[^>]+>", " ", text)
            lines = [self._line_cleanup(l) for l in text.splitlines()]
            cleaned = [l for l in lines if l and not self._line_is_page_number(l)]
            if cleaned:
                pages.append("\n".join(cleaned))
        md = "\n\n".join(pages).strip()
        return md, {
            "total_pages": len(pages),
            "tables_count": 0,
            "extraction_engine": "pypdf",
        }

    def extract_markdown(self, payload: bytes) -> Tuple[str, Dict[str, Any]]:
        # Protect against PDF bombs / huge payloads
        try:
            if int(self.max_payload_bytes) and len(payload or b"") > int(self.max_payload_bytes):
                logger.warning("Payload too large for safe extraction: %d bytes", len(payload or b""))
                return "", {"total_pages": 0, "tables_count": 0, "extraction_engine": "none", "skipped_large_file": True}
        except Exception:
            pass
        errors: List[str] = []
        for engine in self.fallback_order:
            try:
                if engine == "pdfplumber" and self.enable_table_extraction:
                    return self._extract_with_pdfplumber(payload)
                if engine in {"pymupdf", "fitz"}:
                    return self._extract_with_pymupdf(payload)
                if engine == "pypdf":
                    return self._extract_with_pypdf(payload)
            except Exception as exc:
                errors.append(f"{engine}: {exc}")
                logger.warning("PDF extraction engine '%s' failed", engine, exc_info=True)

        # Last resort
        try:
            return self._extract_with_pypdf(payload)
        except Exception as exc:
            errors.append(f"pypdf-final: {exc}")
            logger.exception("All PDF extraction engines failed")
            return "", {"total_pages": 0, "tables_count": 0, "extraction_engine": "none", "errors": errors}


class GenericWebPdfNavigator:
    """Generic navigator to discover PDF links from an arbitrary web page."""

    PDF_KEYWORDS = (
        "pdf",
        "download",
        "telecharger",
        "télécharger",
        "téléchargement",
        "resource",
        "ressource",
        "documentation",
    )

    def __init__(self, downloader: "PdfDownloader"):
        self.downloader = downloader

    def _find_candidates(self, html: str, base_url: str) -> List[Dict[str, str]]:
        soup = BeautifulSoup(html or "", "html.parser")
        candidates: List[Dict[str, str]] = []
        for a in soup.select("a[href]"):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            full = urljoin(base_url, href)
            text = normalize_text(a.get_text(" ", strip=True) or "")
            blob = self.downloader._norm_token(f"{text} {full}")
            # Keep only links that are strong PDF/download candidates.
            is_pdf_like = full.lower().endswith(".pdf") or ".pdf" in full.lower() or "pdf-documentation" in full.lower()
            has_pdf_intent = any(k in blob for k in self.PDF_KEYWORDS)
            if is_pdf_like or has_pdf_intent:
                candidates.append({"url": full, "text": text})

        uniq: List[Dict[str, str]] = []
        seen: set[str] = set()
        for c in candidates:
            key = c["url"].lower()
            if key in seen:
                continue
            seen.add(key)
            uniq.append(c)
        return uniq

    def resolve_targets(self, url: str) -> List[PdfTarget]:
        clean_url = (url or "").strip()
        if clean_url.lower().endswith(".pdf"):
            title = os.path.basename(urlparse(clean_url).path) or "document.pdf"
            return [PdfTarget(source_url=clean_url, pdf_url=clean_url, title_hint=title)]

        resp = self.downloader._request(clean_url)
        html = resp.text or ""
        candidates = self._find_candidates(html, clean_url)
        # Playwright fallback: if no candidates found and rendering is enabled, try a headless render
        if not candidates and self.downloader.render_with_playwright:
            try:
                rendered = self.downloader._render_page_with_playwright(clean_url)
                if rendered:
                    candidates = self._find_candidates(rendered, clean_url)
            except Exception:
                logger.debug("Playwright fallback failed for %s", clean_url, exc_info=True)
        if not candidates:
            return []

        best = self.downloader._select_best_pdf_candidate(candidates)
        if not best:
            return []

        pub_date = (
            self.downloader._extract_date_from_text(best.get("text", ""))
            or self.downloader._extract_date_from_text(best.get("url", ""))
        )
        title = best.get("text") or os.path.basename(urlparse(best["url"]).path) or "document.pdf"
        return [PdfTarget(source_url=clean_url, pdf_url=best["url"], title_hint=title, date_publication=pub_date)]


class AgriInsdNavigator:
    """Plugin navigator for INSD catalog pages (Burkina Faso)."""

    def __init__(self, downloader: "PdfDownloader"):
        self.downloader = downloader
        self.generic = GenericWebPdfNavigator(downloader)

    def _search_bulletin_pages(self, catalog_url: str) -> List[Tuple[int, int, str, str]]:
        resp = self.downloader._request(catalog_url)
        soup = BeautifulSoup(resp.text or "", "html.parser")
        pages: List[Tuple[int, int, str, str]] = []

        for a in soup.select("a[href]"):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            full = urljoin(catalog_url, href)
            text = normalize_text(a.get_text(" ", strip=True) or "")
            blob = self.downloader._norm_token(f"{text} {full}")
            if "bulletin" not in blob:
                continue
            mid = re.search(r"/catalog/(\d+)", full)
            recency = int(mid.group(1)) if mid else 0
            keyword = int("prevision" in blob) * 2 + int("saison" in blob) * 2
            pages.append((keyword, recency, full, text))

        # Enrich with catalog/search endpoint
        try:
            search_url = urljoin(catalog_url, "/index.php/catalog/search")
            sresp = self.downloader.session.get(
                search_url,
                params={
                    "page": 1,
                    "ps": 50,
                    "sk": "bulletin",
                    "sort_by": "year",
                    "sort_order": "desc",
                },
                timeout=self.downloader.policy.timeout,
            )
            sresp.raise_for_status()
            ssoup = BeautifulSoup(sresp.text or "", "html.parser")
            for a in ssoup.select("a[href]"):
                href = (a.get("href") or "").strip()
                if not href:
                    continue
                full = urljoin(catalog_url, href)
                if not re.search(r"/catalog/\d+$", full):
                    continue
                text = normalize_text(a.get_text(" ", strip=True) or "")
                blob = self.downloader._norm_token(f"{text} {full}")
                if "bulletin" not in blob:
                    continue
                mid = re.search(r"/catalog/(\d+)", full)
                recency = int(mid.group(1)) if mid else 0
                keyword = int("prevision" in blob) * 2 + int("saison" in blob) * 2
                pages.append((keyword, recency, full, text))
        except Exception:
            logger.debug("INSD search endpoint failed", exc_info=True)

        pages.sort(reverse=True)
        seen: set[str] = set()
        dedup: List[Tuple[int, int, str, str]] = []
        for row in pages:
            if row[2] in seen:
                continue
            seen.add(row[2])
            dedup.append(row)
        return dedup

    def resolve_targets(self, url: str) -> List[PdfTarget]:
        clean = (url or "").strip()
        if clean.lower().endswith(".pdf"):
            title = os.path.basename(urlparse(clean).path) or "document.pdf"
            return [PdfTarget(source_url=clean, pdf_url=clean, title_hint=title)]

        # Try generic discovery on current page first.
        direct = self.generic.resolve_targets(clean)
        if direct:
            return direct

        for _, _, page_url, title in self._search_bulletin_pages(clean):
            try:
                targets = self.generic.resolve_targets(page_url)
                if not targets:
                    continue
                t = targets[0]
                t.source_url = page_url
                if not t.title_hint:
                    t.title_hint = title
                if not t.date_publication:
                    t.date_publication = self.downloader._extract_date_from_text(title)
                return [t]
            except Exception:
                logger.debug("Failed scanning INSD page %s", page_url, exc_info=True)

        return []


def _retry_download(method: Callable[..., Tuple[bytes, Dict[str, Any]]]) -> Callable[..., Tuple[bytes, Dict[str, Any]]]:
    def wrapper(self: "PdfDownloader", pdf_url: str, *args: Any, **kwargs: Any) -> Tuple[bytes, Dict[str, Any]]:
        attempts = max(1, int(self.download_max_attempts))
        delay = float(self.download_retry_initial_s)
        for idx in range(1, attempts + 1):
            try:
                return method(self, pdf_url, *args, **kwargs)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if idx >= attempts:
                    raise
                logger.warning(
                    "PDF download failed (attempt %d/%d) for %s: %s. Retrying in %.2fs",
                    idx,
                    attempts,
                    pdf_url,
                    exc,
                    delay,
                )
                time.sleep(delay)
                delay = min(self.download_retry_max_s, delay * self.download_retry_multiplier)
    return wrapper


class PdfDownloader(BaseScraper):
    """Downloader orchestrator: target resolution + binary download + extraction."""

    name = "pdf_downloader"
    version = "3.0.0"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config=config)
        cfg = config or {}
        engine_cfg = cfg.get("engine_config") if isinstance(cfg.get("engine_config"), dict) else {}

        self.max_pages = int(cfg.get("pdf_max_pages", 60))
        self.enable_table_extraction = bool(cfg.get("pdf_enable_tables", True))
        self.pdf_fallback_order = [
            str(x).strip().lower()
            for x in (engine_cfg.get("pdf_fallback_order") or ["pdfplumber", "pymupdf", "pypdf"])
            if str(x).strip()
        ]

        self.debug_mode = bool(cfg.get("debug", cfg.get("pdf_debug", False)))
        self.raw_pdf_dir = Path(cfg.get("raw_pdf_dir", "data/raw_pdfs"))

        # New resilience flags
        self.strict_pdf_content_type = bool(cfg.get("strict_pdf_content_type", False))
        self.render_with_playwright = bool(cfg.get("render_with_playwright", False))
        self.playwright_timeout_s = int(cfg.get("playwright_timeout_s", 15))

        # OCR / text-fix options
        self.ocr_enabled = bool(cfg.get("ocr_enabled", False))
        self.min_bytes_for_ocr = int(cfg.get("min_bytes_for_ocr", 200_000))
        self.min_text_chars_for_ocr = int(cfg.get("min_text_chars_for_ocr", 100))
        self.enable_text_fix = bool(cfg.get("enable_text_fix", True))

        # User-Agent rotation
        self.user_agents = list(cfg.get("user_agents") or [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
        ])

        self.download_max_attempts = int(cfg.get("download_max_attempts", 4))
        self.download_retry_initial_s = float(cfg.get("download_retry_initial_s", 1.0))
        self.download_retry_multiplier = float(cfg.get("download_retry_multiplier", 2.0))
        self.download_retry_max_s = float(cfg.get("download_retry_max_s", 12.0))

        # Circuit breaker for domains that return 403/429
        self.circuit_break_seconds = int(cfg.get("circuit_break_seconds", 60 * 10))
        self._circuit: Dict[str, float] = {}

        # Subprocess timeout (seconds) for Playwright / OCR workers
        self.subprocess_timeout_s = int(cfg.get("subprocess_timeout_s", 30))

        # Max payload size to allow in-memory extraction (default 50MB)
        self.max_payload_bytes = int(cfg.get("max_payload_bytes", 50 * 1024 * 1024))

        self.extractor = PdfExtractor(
            max_pages=self.max_pages,
            enable_table_extraction=self.enable_table_extraction,
            fallback_order=self.pdf_fallback_order,
        )

    @staticmethod
    def _norm_token(value: str) -> str:
        txt = (value or "").lower()
        txt = unicodedata.normalize("NFKD", txt)
        txt = "".join(ch for ch in txt if not unicodedata.combining(ch))
        return re.sub(r"\s+", " ", txt).strip()

    @staticmethod
    def _safe_file_stem(value: str) -> str:
        stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", (value or "").strip())
        stem = stem.strip("._")
        return stem or "document_pdf"

    @staticmethod
    def _extract_date_from_text(value: str) -> Optional[str]:
        txt = value or ""
        m_iso = re.search(r"\b(20\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])\b", txt)
        if m_iso:
            yyyy, mm, dd = m_iso.group(1), int(m_iso.group(2)), int(m_iso.group(3))
            return f"{yyyy}-{mm:02d}-{dd:02d}"

        m_dmy = re.search(r"\b(0?[1-9]|[12]\d|3[01])[-/](0?[1-9]|1[0-2])[-/](20\d{2})\b", txt)
        if m_dmy:
            dd, mm, yyyy = int(m_dmy.group(1)), int(m_dmy.group(2)), m_dmy.group(3)
            return f"{yyyy}-{mm:02d}-{dd:02d}"

        m_year = re.search(r"\b(20\d{2})\b", txt)
        if m_year:
            return f"{m_year.group(1)}-01-01"
        return None

    def _score_pdf_candidate(self, item: Dict[str, str]) -> int:
        url = (item.get("url") or "").lower()
        text = item.get("text") or ""
        blob = self._norm_token(text + " " + url)
        score = 0
        if url.endswith(".pdf"):
            score += 4
        if ".pdf" in url:
            score += 2
        if any(k in blob for k in ("download", "telecharger", "télécharger", "pdf", "documentation", "téléchargement")):
            score += 1
        if "pdf-documentation" in url:
            score += 4
        if "documentation" in blob:
            score += 2
        if "get-microdata" in url:
            score -= 6
        score += max(0, 2 - int(len(url) / 200))
        return score

    def _select_best_pdf_candidate(self, candidates: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
        if not candidates:
            return None
        return sorted(candidates, key=lambda c: self._score_pdf_candidate(c), reverse=True)[0]

    def resolve_targets(self, url: str) -> List[PdfTarget]:
        """Discovery layer: can be replaced by site-specific navigators/plugins."""
        parsed = urlparse((url or "").strip())
        host = (parsed.netloc or "").lower()
        path = (parsed.path or "").lower()

        if (url or "").lower().endswith(".pdf"):
            title = os.path.basename(parsed.path) or "document.pdf"
            return [PdfTarget(source_url=url, pdf_url=url, title_hint=title)]

        if "microdata.insd.bf" in host and "/catalog" in path:
            return AgriInsdNavigator(self).resolve_targets(url)

        return GenericWebPdfNavigator(self).resolve_targets(url)

    def _random_user_agent(self) -> str:
        try:
            return random.choice(self.user_agents or [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            ])
        except Exception:
            return "Mozilla/5.0 (compatible; PdfDownloader/1.0)"

    def _render_page_with_playwright(self, url: str) -> Optional[str]:
        """Optional Playwright render fallback. Runs in subprocess with timeout; returns HTML string or None."""
        q = multiprocessing.Queue()
        p = multiprocessing.Process(target=_playwright_worker, args=(q, url, self.playwright_timeout_s))
        p.start()
        p.join(self.subprocess_timeout_s)
        if p.is_alive():
            try:
                p.terminate()
            except Exception:
                pass
            p.join(1)
            logger.warning("Playwright worker timed out and was terminated for %s", url)
            return None

        try:
            status, payload = q.get_nowait()
        except Exception:
            return None

        if status != "ok":
            logger.debug("Playwright worker error for %s: %s", url, payload)
            return None
        return payload

    def _ocr_pdf_bytes(self, payload: bytes) -> Tuple[str, Dict[str, Any]]:
        """Run OCR in a subprocess with timeout to avoid zombie or long-running tasks.
        Returns (text, ex_stats) on success or raises on failure."""
        q = multiprocessing.Queue()
        p = multiprocessing.Process(target=_ocr_worker, args=(q, payload, self.max_pages))
        p.start()
        p.join(self.subprocess_timeout_s)
        if p.is_alive():
            try:
                p.terminate()
            except Exception:
                pass
            p.join(1)
            logger.warning("OCR worker timed out and was terminated")
            raise RuntimeError("OCR timeout")

        try:
            status, payload = q.get_nowait()
        except Exception:
            raise RuntimeError("OCR worker failed without result")

        if status != "ok":
            logger.debug("OCR worker error: %s", payload)
            raise RuntimeError("OCR failed")

        return payload

    @_retry_download
    def _download_pdf_bytes(self, pdf_url: str) -> Tuple[bytes, Dict[str, Any]]:
        # Build rotated headers for the download request
        headers = {
            "User-Agent": self._random_user_agent(), 
            "Accept-Language": "fr-FR,fr;q=0.9",
            "Referer": "https://google.com",
        }

        # Circuit-breaker: if domain recently returned 403/429, skip
        domain = urlparse(pdf_url).netloc.lower()
        nb = self._circuit.get(domain)
        if nb and time.time() < nb:
            raise PermissionError(f"Circuit open for domain {domain}, retry after {int(nb - time.time())}s")

        try:
            response = self.session.get(pdf_url, headers=headers, timeout=self.policy.timeout, allow_redirects=True)
        except Exception:
            # Fallback to generic request helper
            response = self._request(pdf_url)

        payload = response.content or b""
        ctype = (response.headers.get("Content-Type") or "").lower()

        # Circuit breaker on persistent 403/429 to avoid bans
        status = getattr(response, 'status_code', None)
        if status in (403, 429):
            # open circuit for this domain
            self._circuit[domain] = time.time() + float(self.circuit_break_seconds)
            logger.warning("Domain %s opened circuit until %s due to status %s", domain, self._circuit[domain], status)
            raise RuntimeError(f"HTTP {status} for {pdf_url}; opening circuit for domain {domain}")

        # Enforce strict content-type if configured
        if self.strict_pdf_content_type and "application/pdf" not in ctype:
            raise RuntimeError(f"Strict PDF content-type required but got '{ctype}' for {pdf_url}")

        # Some portals return an HTML wrapper on first hop (iframe/embed/download button).
        # Resolve one extra hop to the actual PDF URL when possible.
        if (not payload) or ("html" in ctype):
            html = response.text or ""
            soup = BeautifulSoup(html, "html.parser")
            hop_candidates: List[str] = []

            for tag in soup.select("iframe[src], embed[src], object[data], a[href]"):
                href = (
                    tag.get("src")
                    or tag.get("data")
                    or tag.get("href")
                    or ""
                ).strip()
                if not href:
                    continue
                full = urljoin(pdf_url, href)
                blob = self._norm_token(full + " " + (tag.get_text(" ", strip=True) if hasattr(tag, "get_text") else ""))
                if ".pdf" in full.lower() or any(k in blob for k in ("pdf", "download", "telecharger", "télécharger")):
                    hop_candidates.append(full)

            for hop in hop_candidates[:5]:
                try:
                    hop_headers = {"User-Agent": self._random_user_agent(), "Referer": "https://google.com", "Accept-Language": "fr-FR,fr;q=0.9"}
                    hop_resp = self.session.get(hop, headers=hop_headers, timeout=self.policy.timeout, allow_redirects=True)
                    hop_payload = hop_resp.content or b""
                    hop_ctype = (hop_resp.headers.get("Content-Type") or "").lower()
                    # Strict content-type check for hop as well
                    if self.strict_pdf_content_type and "application/pdf" not in hop_ctype:
                        continue
                    if hop_payload and ("pdf" in hop_ctype or hop.lower().endswith(".pdf") or hop_payload.startswith(b"%PDF")):
                        # verify magic bytes
                        if not hop_payload.startswith(b"%PDF"):
                            continue
                        return hop_payload, {
                            "http_status": hop_resp.status_code,
                            "content_type": hop_ctype,
                            "bytes_downloaded": len(hop_payload),
                            "resolved_pdf_url": hop,
                        }
                except Exception:
                    continue

        if not payload:
            raise RuntimeError("Downloaded PDF is empty")

        # Magic-bytes validation: ensure file looks like a PDF
        if not payload.startswith(b"%PDF"):
            raise RuntimeError("Downloaded file does not start with %PDF - invalid or wrapped content")

        return payload, {
            "http_status": response.status_code,
            "content_type": ctype,
            "bytes_downloaded": len(payload),
        }

    def _persist_pdf(self, payload: bytes, pdf_url: str, title_hint: str) -> Path:
        self.raw_pdf_dir.mkdir(parents=True, exist_ok=True)
        parsed = urlparse(pdf_url)
        filename = os.path.basename(parsed.path) or ""
        if not filename.lower().endswith(".pdf"):
            filename = f"{self._safe_file_stem(title_hint)}.pdf"
        out = self.raw_pdf_dir / filename
        out.write_bytes(payload)
        return out

    def scrape(self, url: str) -> Tuple[Optional[RawDocument], Dict[str, Any]]:
        targets = self.resolve_targets(url)
        if not targets:
            return None, {"http_status": None, "bytes_downloaded": 0, "error": "no_pdf_target"}

        target = targets[0]
        payload, dl_meta = self._download_pdf_bytes(target.pdf_url)
        local_path = self._persist_pdf(payload, target.pdf_url, target.title_hint)

        markdown, ex_stats = self.extractor.extract_markdown(payload)
        # Normalize whitespace and run optional ftfy text-fix
        try:
            markdown = normalize_text(markdown)
            if self.enable_text_fix:
                try:
                    import ftfy  # type: ignore

                    try:
                        markdown = ftfy.fix_text(markdown)
                    except Exception:
                        logger.debug("ftfy.fix_text failed, continuing with un-fixed text", exc_info=True)
                except Exception:
                    logger.debug("ftfy not installed; skipping text-fix")
        except Exception:
            markdown = (markdown or "")

        # Detect scanned PDF -> OCR path
        needs_ocr = False
        try:
            bytes_len = int(dl_meta.get("bytes_downloaded", 0))
        except Exception:
            bytes_len = 0

        if (len((markdown or "").strip()) < int(self.min_text_chars_for_ocr)) and (bytes_len >= int(self.min_bytes_for_ocr)):
            needs_ocr = True

        if needs_ocr:
            # If OCR is enabled, perform OCR and replace markdown
            if self.ocr_enabled:
                try:
                    ocr_text, ocr_stats = self._ocr_pdf_bytes(payload)
                    ocr_text = normalize_text(ocr_text or "")
                    if self.enable_text_fix:
                        try:
                            import ftfy  # type: ignore

                            try:
                                ocr_text = ftfy.fix_text(ocr_text)
                            except Exception:
                                logger.debug("ftfy.fix_text failed on OCR text", exc_info=True)
                        except Exception:
                            logger.debug("ftfy not installed; skipping text-fix on OCR text")

                    markdown = ocr_text
                    ex_stats = ex_stats or {}
                    ex_stats.update(ocr_stats or {})
                    ex_stats["extraction_engine"] = ex_stats.get("extraction_engine", "ocr")
                    ex_stats["ocr_performed"] = True
                except Exception:
                    logger.exception("OCR processing failed; marking needs_ocr")
                    return None, {
                        "http_status": dl_meta.get("http_status"),
                        "bytes_downloaded": dl_meta.get("bytes_downloaded", 0),
                        "error": "needs_ocr",
                        "pdf_url": target.pdf_url,
                        "needs_ocr": True,
                    }
            else:
                return None, {
                    "http_status": dl_meta.get("http_status"),
                    "bytes_downloaded": dl_meta.get("bytes_downloaded", 0),
                    "error": "needs_ocr",
                    "pdf_url": target.pdf_url,
                    "needs_ocr": True,
                }

        if not markdown.strip():
            return None, {
                "http_status": dl_meta.get("http_status"),
                "bytes_downloaded": dl_meta.get("bytes_downloaded", 0),
                "error": "empty_extraction",
                "pdf_url": target.pdf_url,
            }

        content_hash = content_sha256(markdown)
        date_pub = (
            target.date_publication
            or self._extract_date_from_text(target.title_hint)
            or self._extract_date_from_text(markdown[:2000])
        )

        metadata = {
            "source_type": "pdf",
            "source_url": target.source_url,
            "pdf_url": target.pdf_url,
            "date_publication": date_pub,
            "total_pages": int(ex_stats.get("total_pages", 0)),
            "extraction_engine": ex_stats.get("extraction_engine", "unknown"),
            "tables_count": int(ex_stats.get("tables_count", 0)),
            "content_hash": content_hash,
            "downloaded_file": str(local_path),
            "needs_ocr": bool(ex_stats.get("ocr_performed") is not True and len((markdown or "").strip()) < int(self.min_text_chars_for_ocr)),
            "ocr_performed": bool(ex_stats.get("ocr_performed", False)),
        }

        doc = self._build_document(
            url=target.pdf_url,
            title=target.title_hint or "document.pdf",
            markdown=markdown,
            metadata=metadata,
        )

        preview = markdown[:500]
        if self.debug_mode:
            logger.info("PDF debug preview (500 chars): %s", preview)

        log_meta = {
            "http_status": dl_meta.get("http_status"),
            "bytes_downloaded": dl_meta.get("bytes_downloaded", 0),
            "pdf_url": target.pdf_url,
            "source_url": target.source_url,
            "total_pages": metadata["total_pages"],
            "extraction_engine": metadata["extraction_engine"],
            "content_hash": content_hash,
            "debug_preview": preview if self.debug_mode else None,
        }

        return doc, log_meta

    def handle(self, record: Dict[str, Any]) -> Tuple[Optional[RawDocument], Dict[str, Any]]:
        """Ingest one discovery record in real-time.

        Expected minimal shape:
        {
          "pdf_url": "https://...",
          "metadata": {...},
          "parent_page_url": "https://...",
          "context": {...}
        }
        """
        payload = dict(record or {})
        pdf_url = str(payload.get("pdf_url") or payload.get("url") or "").strip()
        if not pdf_url:
            return None, {
                "error": "missing_pdf_url",
                "http_status": None,
                "bytes_downloaded": 0,
            }

        try:
            doc, meta = self.scrape(pdf_url)
        except Exception as exc:
            return None, {
                "error": str(exc),
                "pdf_url": pdf_url,
                "http_status": None,
                "bytes_downloaded": 0,
            }
        if doc is None:
            out = dict(meta or {})
            out["pdf_url"] = pdf_url
            return None, out

        discovery_meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
        parent_page_url = payload.get("parent_page_url")

        doc_meta = dict(doc.metadata or {})
        doc_meta["discovery_parent_page_url"] = parent_page_url
        doc_meta["discovery_context"] = context
        doc_meta["discovery_metadata"] = discovery_meta
        if "confidence_score" in discovery_meta:
            # Promote confidence score for downstream assertions and analytics.
            doc_meta["confidence_score"] = discovery_meta.get("confidence_score")
        doc.metadata = doc_meta

        out = dict(meta or {})
        out["pdf_url"] = pdf_url
        out["discovery_confidence_score"] = discovery_meta.get("confidence_score")
        return doc, out
