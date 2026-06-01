from __future__ import annotations

import json

from agriconnect.services.scraper.scrapers.pdf_discovery import (
    DiscoveryWriter,
    is_pdf_candidate,
    is_wordpress_pdf,
    normalize_pdf_url,
)


def test_normalize_pdf_url_removes_query_and_fragment() -> None:
    raw = "https://example.org/reports/doc.pdf?download=1#section"
    assert normalize_pdf_url(raw) == "https://example.org/reports/doc.pdf"


def test_pdf_candidate_patterns() -> None:
    assert is_pdf_candidate("https://example.org/a.pdf")
    assert is_pdf_candidate("https://example.org/download/report")
    assert is_pdf_candidate("https://example.org/wp-content/uploads/2026/04/report")
    assert not is_pdf_candidate("https://example.org/news/article")


def test_wordpress_pdf_detection() -> None:
    assert is_wordpress_pdf("https://example.org/wp-content/uploads/2026/04/report.pdf")
    assert not is_wordpress_pdf("https://example.org/download/report.pdf")


def test_discovery_writer_deduplicates_normalized_urls(tmp_path) -> None:
    out = tmp_path / "mapping.ndjson"
    writer = DiscoveryWriter(str(out))

    first = {
        "pdf_url": "https://example.org/files/a.pdf?x=1",
        "parent_page_url": "https://example.org/news/1",
        "context": {"page_title": "T", "anchor_text": "A", "surrounding_text": "S"},
        "metadata": {"site_name": "example.org", "discovery_method": "crawl", "extracted_date": None},
    }
    second = {
        "pdf_url": "https://example.org/files/a.pdf",
        "parent_page_url": "https://example.org/news/2",
        "context": {"page_title": "T2", "anchor_text": "A2", "surrounding_text": "S2"},
        "metadata": {"site_name": "example.org", "discovery_method": "crawl", "extracted_date": None},
    }

    assert writer.write(first) is True
    assert writer.write(second) is False
    assert writer.seen_count == 1

    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["pdf_url"] == "https://example.org/files/a.pdf"
