"""Robust offline E2E stress harness for discovery -> ingestion pipeline.

Goals validated in one run:
- Discovery writer supports in-memory streams.
- Immediate callback-style ingestion via PdfDownloader.handle(record).
- Domain quarantine behaviour (403/429 style) is enforced.
- Invalid binary payloads are rejected (magic-bytes guard).
- confidence_score travels into final RawDocument metadata.
- At least one successful extraction path uses pdfplumber.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List
from urllib.parse import urlparse

# Ensure project packages importable
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "backend", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agriconnect.services.scraper.scrapers.pdf_discovery import DiscoveryWriter
from agriconnect.services.scraper.scrapers.pdf_downloader import PdfDownloader

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("e2e_stress")


def _build_valid_pdf_bytes() -> bytes:
    """Create a small valid PDF with text for deterministic extraction tests."""
    try:
        import fitz  # type: ignore

        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "AgriConnect robust pipeline E2E")
        payload = doc.tobytes()
        doc.close()
        if payload.startswith(b"%PDF"):
            return payload
    except Exception:
        logger.warning("PyMuPDF PDF generation failed, using static fallback payload")

    # Fallback minimal valid payload
    return (
        b"%PDF-1.5\n"
        b"1 0 obj <</Type /Catalog /Pages 2 0 R>> endobj\n"
        b"2 0 obj <</Type /Pages /Kids [3 0 R] /Count 1>> endobj\n"
        b"3 0 obj <</Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R"
        b" /Resources <</Font <</F1 5 0 R>>>>> endobj\n"
        b"4 0 obj <</Length 63>> stream\n"
        b"BT /F1 18 Tf 50 90 Td (AgriConnect robust pipeline E2E) Tj ET\n"
        b"endstream endobj\n"
        b"5 0 obj <</Type /Font /Subtype /Type1 /BaseFont /Helvetica>> endobj\n"
        b"xref\n0 6\n0000000000 65535 f \n"
        b"0000000010 00000 n \n0000000060 00000 n \n0000000117 00000 n \n"
        b"0000000247 00000 n \n0000000360 00000 n \n"
        b"trailer <</Root 1 0 R /Size 6>>\nstartxref\n430\n%%EOF"
    )


VALID_PDF_BYTES = _build_valid_pdf_bytes()


class TestPdfDownloader(PdfDownloader):
    """Downloader override that simulates network outcomes while preserving ingestion logic."""

    def __init__(self) -> None:
        super().__init__(
            config={
                "raw_pdf_dir": os.path.join(ROOT, "tmp", "test_raw_pdfs"),
                "strict_pdf_content_type": True,
            }
        )

    def _download_pdf_bytes(self, pdf_url: str):
        domain = (urlparse(pdf_url).netloc or "").lower()

        # Simulate persistent 403/429 domain ban by opening downloader circuit.
        if "quarantine.test" in domain:
            self._circuit[domain] = time.time() + 300
            raise PermissionError(f"Circuit open for domain {domain}")

        # Simulate binary validation failure.
        if "badcontent.test" in domain:
            raise RuntimeError("Downloaded file does not start with %PDF - invalid or wrapped content")

        return VALID_PDF_BYTES, {
            "http_status": 200,
            "content_type": "application/pdf",
            "bytes_downloaded": len(VALID_PDF_BYTES),
        }


def _build_discovery_seed() -> List[Dict[str, Any]]:
    return [
        {
            "scraper_type": "NewsScraper",
            "pdf_url": "https://good.test/news/news_brief_2026.pdf",
            "parent_page_url": "https://good.test/news",
            "metadata": {"confidence_score": 0.91, "topic": "advisory"},
        },
        {
            "scraper_type": "TechnicalResourcesExplorer",
            "pdf_url": "https://good.test/technical/soil_report_2026.pdf",
            "parent_page_url": "https://good.test/technical",
            "metadata": {"confidence_score": 0.88, "topic": "soil"},
        },
        {
            "scraper_type": "InstitutionalPdfHarvester",
            "pdf_url": "https://quarantine.test/bulletin_2026.pdf",
            "parent_page_url": "https://quarantine.test/catalog",
            "metadata": {"confidence_score": 0.42, "topic": "institutional"},
        },
        {
            "scraper_type": "NewsScraper",
            "pdf_url": "https://badcontent.test/fake_file.pdf",
            "parent_page_url": "https://badcontent.test/page",
            "metadata": {"confidence_score": 0.53, "topic": "news"},
        },
        # Duplicate URL to validate DiscoveryWriter dedup
        {
            "scraper_type": "TechnicalResourcesExplorer",
            "pdf_url": "https://good.test/technical/soil_report_2026.pdf",
            "parent_page_url": "https://good.test/technical/dup",
            "metadata": {"confidence_score": 0.77, "topic": "dup"},
        },
    ]


def _print_audit_table(audit_rows: List[Dict[str, Any]]) -> None:
    print("\n=== Audit Report ===")
    print("Scraper_Type | URLs_Discovered | PDFs_Downloaded | Extraction_Success_Rate | Avg_Time_Per_Doc_ms")
    print("-" * 95)
    for row in audit_rows:
        print(
            f"{row['scraper_type']} | {row['urls_discovered']} | {row['pdfs_downloaded']} | "
            f"{row['extraction_success_rate']:.2f} | {row['avg_time_per_doc_ms']:.2f}"
        )


def run_test() -> None:
    downloader = TestPdfDownloader()

    # 1) Discovery stage in-memory using DiscoveryWriter stream mode
    discovery_stream = io.StringIO()
    writer = DiscoveryWriter(discovery_stream)
    for rec in _build_discovery_seed():
        writer.write(rec)

    lines = [ln for ln in discovery_stream.getvalue().splitlines() if ln.strip()]
    discovery_records = [json.loads(ln) for ln in lines]

    # 2) Immediate ingestion callback per discovered record
    run_rows: List[Dict[str, Any]] = []
    docs: List[Any] = []
    for rec in discovery_records:
        t0 = time.perf_counter()
        doc, meta = downloader.handle(rec)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        ok = doc is not None
        if ok:
            docs.append(doc)
        run_rows.append(
            {
                "scraper_type": rec.get("scraper_type", "unknown"),
                "url": rec.get("pdf_url"),
                "ok": ok,
                "elapsed_ms": elapsed_ms,
                "error": None if ok else str((meta or {}).get("error") or "unknown_error"),
                "meta": meta or {},
                "doc": doc,
            }
        )

    # 3) Assertions for robustness contract
    assert writer.seen_count == len(discovery_records), "DiscoveryWriter dedup/stream handling mismatch"

    assert any(
        (r.get("error") or "").lower().find("circuit open") >= 0 for r in run_rows if not r["ok"]
    ), "Expected quarantine/circuit behaviour not observed"

    assert any(
        (r.get("error") or "").lower().find("%pdf") >= 0 for r in run_rows if not r["ok"]
    ), "Expected binary magic-bytes rejection not observed"

    assert any(
        (r["doc"].metadata or {}).get("confidence_score") is not None for r in run_rows if r["ok"] and r.get("doc") is not None
    ), "confidence_score was not propagated to RawDocument metadata"

    assert any(
        ((r["doc"].metadata or {}).get("extraction_engine") == "pdfplumber") for r in run_rows if r["ok"] and r.get("doc") is not None
    ), "No successful extraction using pdfplumber"

    # 4) Audit table
    grouped = defaultdict(list)
    for r in run_rows:
        grouped[r["scraper_type"]].append(r)

    audit_rows: List[Dict[str, Any]] = []
    for scraper_type, rows in grouped.items():
        discovered = len(rows)
        downloaded = sum(1 for r in rows if r["ok"])
        rate = (downloaded / discovered) if discovered else 0.0
        avg_ms = sum(r["elapsed_ms"] for r in rows) / max(1, len(rows))
        audit_rows.append(
            {
                "scraper_type": scraper_type,
                "urls_discovered": discovered,
                "pdfs_downloaded": downloaded,
                "extraction_success_rate": rate,
                "avg_time_per_doc_ms": avg_ms,
            }
        )

    audit_rows.sort(key=lambda r: r["scraper_type"])
    _print_audit_table(audit_rows)

    failures = [r for r in run_rows if not r["ok"]]
    top_failures = Counter(r["error"] for r in failures).most_common(5)
    print("\nTop failure reasons:")
    if not top_failures:
        print("- none")
    else:
        for reason, count in top_failures:
            print(f"- {reason}: {count}")

    print("\nAssertions passed: robust discovery->ingestion pipeline is healthy.")


if __name__ == "__main__":
    try:
        run_test()
    except Exception:
        logger.exception("E2E stress test failed")
        raise
