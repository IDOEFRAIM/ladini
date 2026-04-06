from __future__ import annotations

import io
import logging
import re
from typing import Dict, Optional, Tuple, List, Any

# Core libraries
from pypdf import PdfReader

# Core AgriConnect
from agriconnect.core.schemas import RawDocument, ScraperLog
from agriconnect.core.scraper_utils import normalize_text

from .base import BaseScraper
from .registry import register_scraper

logger = logging.getLogger(__name__)

@register_scraper("pdf", "pdf_document")
class PdfDownloader(BaseScraper):
    """
    High-performance PDF extractor (V2.1 "Industrial").
    Supports: text extraction, table reconstruction, and visual analysis.
    """

    name = "pdf_downloader"
    version = "2.1.0"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config=config)
        cfg = config or {}
        # Paramètres de contrôle
        self.max_pages = int(cfg.get("pdf_max_pages", 60))
        # Fast mode: True => Turbo (pypdf only). Default: False (Gold Data)
        self.fast_mode = bool(cfg.get("fast_mode", cfg.get("pdf_fast_mode", False)))
        self.enable_table_extraction = bool(cfg.get("pdf_enable_tables", True)) and not self.fast_mode
        self.enable_selective_vision = bool(cfg.get("pdf_enable_vision", False))
        
        # Seuils de détection visuelle
        self.min_img_w = int(cfg.get("pdf_min_img_width", 300))
        self.min_img_h = int(cfg.get("pdf_min_img_height", 300))

    @staticmethod
    def _to_markdown_table(rows: List[List[Any]]) -> str:
        """Convert a list-of-lists into a clean Markdown table."""
        if not rows or not any(rows):
            return ""
        
        # Nettoyage et normalisation des cellules
        cleaned_rows = []
        for row in rows:
            cleaned_rows.append([normalize_text(str(cell or "")) for cell in row])

        # Calcul de la largeur (nombre de colonnes)
        width = max(len(r) for r in cleaned_rows)
        
        # Construction des lignes
        lines = []
        # Header strict: never shift data rows to guess a header.
        raw_header = cleaned_rows[0]
        if all(not (c and c.strip()) for c in raw_header):
            header = [f"Col {i+1}" for i in range(width)]
        else:
            header = raw_header + [""] * (width - len(raw_header))
        lines.append("| " + " | ".join(header) + " |")
        # Separator
        lines.append("| " + " | ".join(["---"] * width) + " |")
        # Body
        for row in cleaned_rows[1:]:
            full_row = row + [""] * (width - len(row))
            lines.append("| " + " | ".join(full_row) + " |")
            
        return "\n".join(lines)

    @staticmethod
    def _row_type_signature(row: List[Any]) -> List[str]:
        """Return a per-cell coarse type signature: numeric, text, or empty."""
        signature: List[str] = []
        for cell in row:
            value = normalize_text(str(cell or "")).strip()
            if not value:
                signature.append("empty")
                continue
            compact = value.replace(" ", "").replace(",", "")
            try:
                float(compact)
                signature.append("numeric")
            except Exception:
                signature.append("text")
        return signature

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        """Best-effort float conversion to avoid hard failures on malformed PDFs."""
        try:
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _infer_column_spans(ncols: int, cells: List[Any], table_bbox: Optional[Tuple[float, float, float, float]]) -> List[Tuple[float, float]]:
        """Infer column x spans with a lightweight strategy optimized for throughput."""
        if ncols <= 0:
            return []

        # Fast path: use table bbox for a uniform split (stable and cheap).
        if table_bbox is not None:
            x0 = PdfDownloader._safe_float(table_bbox[0])
            x1 = PdfDownloader._safe_float(table_bbox[2])
            width = max(x1 - x0, 1.0)
            col_w = width / float(ncols)
            return [(x0 + idx * col_w, x0 + (idx + 1) * col_w) for idx in range(ncols)]

        # Fallback: dedupe rough spans from cell boxes.
        spans: List[Tuple[float, float]] = []
        for cell in cells or []:
            if isinstance(cell, dict):
                x0 = PdfDownloader._safe_float(cell.get("x0"))
                x1 = PdfDownloader._safe_float(cell.get("x1"))
            else:
                x0 = PdfDownloader._safe_float(cell[0]) if len(cell) > 0 else 0.0
                x1 = PdfDownloader._safe_float(cell[2]) if len(cell) > 2 else 0.0
            if x1 <= x0:
                continue
            if not any(abs(a - x0) <= 3.0 and abs(b - x1) <= 3.0 for a, b in spans):
                spans.append((x0, x1))
            if len(spans) >= ncols:
                break
        return spans

    def _is_same_structure(self, table_a: Dict[str, Any], table_b: Dict[str, Any], tolerance_pt: float = 3.0) -> bool:
        """Fuzzy structure matching for cross-page table stitching.

        Rules:
        - Same number of columns.
        - Similar column x spans within tolerance.
        - Optional row-type coherence between tail(A) and head(B).
        """
        ncols_a = int(table_a.get("ncols", 0))
        ncols_b = int(table_b.get("ncols", 0))
        if ncols_a <= 0 or ncols_a != ncols_b:
            return False

        spans_a = table_a.get("col_spans") or []
        spans_b = table_b.get("col_spans") or []
        if len(spans_a) == ncols_a and len(spans_b) == ncols_b:
            for idx in range(ncols_a):
                a0, a1 = spans_a[idx]
                b0, b1 = spans_b[idx]
                if abs(float(a0) - float(b0)) > tolerance_pt or abs(float(a1) - float(b1)) > tolerance_pt:
                    return False

        sig_a = table_a.get("tail_signature")
        sig_b = table_b.get("head_signature")
        if isinstance(sig_a, list) and isinstance(sig_b, list) and sig_a and sig_b:
            pairs = min(len(sig_a), len(sig_b))
            if pairs > 0:
                comparable = 0
                aligned = 0
                for idx in range(pairs):
                    if sig_a[idx] == "empty" or sig_b[idx] == "empty":
                        continue
                    comparable += 1
                    if sig_a[idx] == sig_b[idx]:
                        aligned += 1
                # Weighted signal, not a hard absolute rule.
                if comparable >= 3 and (aligned / comparable) < 0.34:
                    return False

        return True

    def _merge_tables(self, rows_a: List[List[Any]], rows_b: List[List[Any]]) -> Tuple[List[List[Any]], bool]:
        """Merge two table row matrices and remove repeated header on continuation pages."""
        if not rows_a:
            return rows_b, False
        if not rows_b:
            return rows_a, False

        header_a = [normalize_text(str(c or "")).strip() for c in rows_a[0]]
        first_b = [normalize_text(str(c or "")).strip() for c in rows_b[0]]

        strip_header = False
        if header_a and first_b:
            width = max(len(header_a), len(first_b))
            left = header_a + [""] * (width - len(header_a))
            right = first_b + [""] * (width - len(first_b))
            strip_header = left == right

        if strip_header:
            return rows_a + rows_b[1:], True
        return rows_a + rows_b, False

    def _flush_table(self, table_dict: Dict[str, Any]) -> str:
        """Convert pending table buffer into a labeled Markdown block."""
        rows = table_dict.get("rows") or []
        md = self._to_markdown_table(rows)
        if not md:
            return ""
        p0 = int(table_dict.get("page_start", 0) or 0)
        p1 = int(table_dict.get("page_end", p0) or p0)
        if p0 and p1 and p0 != p1:
            label = f"Table (Pages {p0}-{p1})"
        else:
            page_ref = p0 if p0 else p1
            label = f"Table (Page {page_ref})"
        return f"\n### {label}\n\n{md}"

    def _extract_markdown(self, payload: bytes) -> Tuple[str, Dict[str, Any]]:
        """
        Hybrid extraction engine.
        Returns (content_markdown, extraction_stats).
        """
        out = io.StringIO()
        first_block = True
        stats = {
            "pages": 0,
            "tables_count": 0,
            "stitched_tables_count": 0,
            "images_count": 0,
            "image_extraction_errors": 0,
        }

        def append_block(block: str) -> None:
            nonlocal first_block
            if not block:
                return
            if not first_block:
                out.write("\n\n---\n\n")
            out.write(block)
            first_block = False

        # Turbo / Fast mode: use pypdf only for maximum throughput
        if self.fast_mode:
            try:
                reader = PdfReader(io.BytesIO(payload))
                for i, page in enumerate(reader.pages[:self.max_pages], start=1):
                    text = page.extract_text() or ""
                    append_block(f"## Page {i}\n\n{normalize_text(text)}")
                    stats["pages"] = i
                stats["engine_used"] = "pypdf"
                stats["extraction_quality"] = "low"
                return out.getvalue(), stats
            except Exception:
                logger.exception("pypdf extraction failed in fast_mode; returning empty content.")
                return "", stats

        # Gold Data mode: try pdfplumber for high-quality extraction (tables + text + visual info)
        engine_used = "pdfplumber"
        extraction_quality = "high"
        if self.enable_table_extraction:
            try:
                # Lazy-load heavy dependency inside method to avoid import-time cost.
                import pdfplumber  # type: ignore

                pending_table_buffer: Optional[Dict[str, Any]] = None

                with pdfplumber.open(io.BytesIO(payload)) as pdf:
                    # Collect per-page entries first so we can detect and remove repeated
                    # headers/footers that appear on multiple pages (page numbers, report title, etc.).
                    page_entries: List[Dict[str, Any]] = []

                    for i, page in enumerate(pdf.pages[:self.max_pages], start=1):
                        page_blocks: List[str] = []

                        # 1. Text extraction
                        page_text = ""
                        try:
                            text = page.extract_text() or ""
                            if text.strip():
                                page_text = normalize_text(text)
                        except Exception:
                            logger.debug("pdfplumber page.extract_text() failed on page %s", i)

                        # 2. Table extraction + stitching
                        current_tables: List[Dict[str, Any]] = []
                        try:
                            found_tables = page.find_tables() or []
                            for ft in found_tables:
                                rows = ft.extract() or []
                                ncols = max((len(r) for r in rows), default=0)
                                if ncols <= 0:
                                    continue
                                col_spans = self._infer_column_spans(
                                    ncols=ncols,
                                    cells=list(getattr(ft, "cells", []) or []),
                                    table_bbox=getattr(ft, "bbox", None),
                                )
                                current_tables.append(
                                    {
                                        "rows": rows,
                                        "ncols": ncols,
                                        "col_spans": col_spans,
                                        "head_signature": self._row_type_signature(rows[0]) if rows else [],
                                        "tail_signature": self._row_type_signature(rows[-1]) if rows else [],
                                        "page_start": i,
                                        "page_end": i,
                                    }
                                )
                        except Exception:
                            current_tables = []

                        # Handle pending table from previous page.
                        if pending_table_buffer is not None and not current_tables:
                            flushed = self._flush_table(pending_table_buffer)
                            if flushed:
                                page_blocks.append(flushed)
                                stats["tables_count"] += 1
                            pending_table_buffer = None

                        if pending_table_buffer is not None and current_tables:
                            if self._is_same_structure(pending_table_buffer, current_tables[0]):
                                merged_rows, _ = self._merge_tables(pending_table_buffer["rows"], current_tables[0]["rows"])
                                merged_table = {
                                    "rows": merged_rows,
                                    "ncols": pending_table_buffer["ncols"],
                                    "col_spans": pending_table_buffer.get("col_spans", []),
                                    "head_signature": pending_table_buffer.get("head_signature", []),
                                    "tail_signature": self._row_type_signature(merged_rows[-1]) if merged_rows else [],
                                    "page_start": pending_table_buffer.get("page_start", i),
                                    "page_end": i,
                                }
                                current_tables = [merged_table] + current_tables[1:]
                                stats["stitched_tables_count"] += 1
                                logger.debug(
                                    "Table stitched successfully from page %s to page %s",
                                    pending_table_buffer.get("page_start", i),
                                    i,
                                )
                                pending_table_buffer = None
                            else:
                                flushed = self._flush_table(pending_table_buffer)
                                if flushed:
                                    page_blocks.append(flushed)
                                    stats["tables_count"] += 1
                                pending_table_buffer = None

                        # Emit all but last table and keep last as pending for next page stitching.
                        for idx, table_info in enumerate(current_tables):
                            is_last = idx == (len(current_tables) - 1)
                            if is_last:
                                pending_table_buffer = table_info
                                continue
                            md_t = self._flush_table(table_info)
                            if md_t:
                                page_blocks.append(md_t)
                                stats["tables_count"] += 1

                        # 3. Selective visual detection (images)
                        if self.enable_selective_vision:
                            try:
                                images = getattr(page, "images", []) or []
                                for img in images:
                                    # Attempt to get width/height; fall back to bbox
                                    x0 = self._safe_float(img.get("x0"))
                                    x1 = self._safe_float(img.get("x1"))
                                    y0 = self._safe_float(img.get("y0"))
                                    y1 = self._safe_float(img.get("y1"))
                                    w_f = self._safe_float(img.get("width"), max(x1 - x0, 0.0))
                                    h_f = self._safe_float(img.get("height"), max(y1 - y0, 0.0))
                                    if w_f >= self.min_img_w and h_f >= self.min_img_h:
                                        label = "POTENTIAL_TABLE_IMAGE" if (h_f > 0 and (w_f / h_f) >= 1.8) else "GRAPHIC"
                                        page_blocks.append(f"\n> [INFO_VISUAL: {label} {int(w_f)}x{int(h_f)}pt detected]")
                                        stats["images_count"] += 1
                            except Exception as exc:
                                stats["image_extraction_errors"] += 1
                                logger.warning("Selective vision failed on page %s: %s", i, exc, exc_info=True)

                        # Keep page text close to page context after table handling.
                        if page_text:
                            page_blocks.append(page_text)

                        page_entries.append({
                            "page": i,
                            "blocks": page_blocks,
                            "text": page_text,
                        })
                        stats["pages"] = i

                    # After collecting pages, detect repeated header/footer lines and clean per-page text
                    # Build candidate lines from first/last N lines of each page
                    first_last_candidates: Dict[str, int] = {}
                    for entry in page_entries:
                        lines = [l.strip() for l in (entry.get("text") or "").splitlines() if l.strip()]
                        head = lines[:3]
                        tail = lines[-3:]
                        for l in head + tail:
                            if not l:
                                continue
                            key = l.lower()
                            first_last_candidates[key] = first_last_candidates.get(key, 0) + 1

                    # Any candidate appearing on >=2 pages is considered a repeated header/footer
                    repeated_lines = {k for k, v in first_last_candidates.items() if v >= 2}

                    # Regex patterns for obvious page numbers and common footer words
                    page_num_re = re.compile(r"^\s*(page\s*\d+(\s*/\s*\d+)?|page\s*\d+\s*sur\s*\d+)\s*$", re.I)
                    common_footer_words = re.compile(r"\b(minist[eè]re|ministere|©|copyright|tous droits|imprim[eé])\b", re.I)
                    # Inline page number pattern to strip inside tables/lines
                    page_num_inline_re = re.compile(r"page\s*\d+(\s*(/|sur)\s*\d+)?", re.I)

                    # Emit cleaned page blocks
                    for entry in page_entries:
                        i = entry.get("page")
                        cleaned_lines: List[str] = []
                        for raw_line in (entry.get("text") or "").splitlines():
                            line = raw_line.strip()
                            if not line:
                                continue
                            key = line.lower()
                            # Drop explicit page number lines
                            if page_num_re.match(line):
                                continue
                            # Drop lines that match known footer words and are short
                            if common_footer_words.search(line) and len(line) < 80:
                                continue
                            # Drop repeated header/footer lines detected across pages
                            if key in repeated_lines and len(line) < 120:
                                continue
                            cleaned_lines.append(line)

                        cleaned_text = "\n".join(cleaned_lines)
                        # Remove inline page tokens that may be embedded inside tables or lines
                        cleaned_text = page_num_inline_re.sub("", cleaned_text)
                        # Reconstruct blocks: start with page header, then any tables/images, then cleaned text
                        blocks = [f"## Page {i}"]
                        for b in entry.get("blocks") or []:
                            # skip raw text that was included in blocks as we'll append cleaned_text last
                            if b.strip() == (entry.get("text") or "").strip():
                                continue
                            # Strip inline page tokens from block content as well
                            safe_b = page_num_inline_re.sub("", b)
                            blocks.append(safe_b)
                        if cleaned_text:
                            blocks.append(cleaned_text)
                        append_block("\n\n".join(blocks))

                    # Flush remaining pending table after last page.
                    if pending_table_buffer is not None:
                        pending_md = self._flush_table(pending_table_buffer)
                        if pending_md:
                            append_block(pending_md)
                            stats["tables_count"] += 1

            except ImportError:
                logger.warning("pdfplumber not installed; falling back to pypdf (text-only)")
                engine_used = "pypdf"
                extraction_quality = "low"
                self.enable_table_extraction = False
            except Exception:
                logger.exception("pdfplumber processing failed, falling back to pypdf")
                engine_used = "pypdf"
                extraction_quality = "low"
                self.enable_table_extraction = False

        # If table extraction is disabled or failed, use pypdf for text-only extraction
        if not self.enable_table_extraction:
            try:
                reader = PdfReader(io.BytesIO(payload))
                for i, page in enumerate(reader.pages[:self.max_pages], start=1):
                    text = page.extract_text() or ""
                    append_block(f"## Page {i}\n\n{normalize_text(text)}")
                    stats["pages"] = i
                # If we reached here and engine not set to pdfplumber, ensure metadata
                if engine_used != "pypdf":
                    engine_used = "pypdf"
                    extraction_quality = "low"
            except Exception:
                logger.exception("pypdf fallback extraction failed")

        # Attach engine/quality to stats for caller usage
        stats["engine_used"] = engine_used
        stats["extraction_quality"] = extraction_quality

        return out.getvalue(), stats

    def scrape(self, url: str) -> Tuple[Optional[RawDocument], Dict[str, Any]]:
        """Main entrypoint orchestrated by BaseScraper."""
        response = self._request(url)
        payload = response.content or b""
        markdown, stats = self._extract_markdown(payload)

        if not markdown.strip():
            return None, {
                "http_status": response.status_code,
                "bytes_downloaded": len(payload),
                "error": "empty_extraction"
            }

        # Build enriched metadata
        metadata = {
            "source_type": "pdf",
            "engine_used": stats.get("engine_used", "pypdf"),
            "extraction_quality": stats.get("extraction_quality", "low"),
            "image_size_unit": "pt",
            "stats": {
                "pages": int(stats.get("pages", 0)),
                "tables_count": int(stats.get("tables_count", 0)),
                "stitched_tables_count": int(stats.get("stitched_tables_count", 0)),
                "images_count": int(stats.get("images_count", 0)),
                "image_extraction_errors": int(stats.get("image_extraction_errors", 0)),
            },
            "content_type": response.headers.get("Content-Type", "")
        }

        doc = self._build_document(
            url=url,
            title=url.split("/")[-1].replace("%20", " ") or "document.pdf",
            markdown=markdown,
            metadata=metadata,
        )

        log_meta = {
            "http_status": response.status_code,
            "bytes_downloaded": len(payload),
            "engine_used": metadata["engine_used"],
            "extraction_quality": metadata["extraction_quality"],
            "stats": metadata["stats"],
        }

        return doc, log_meta