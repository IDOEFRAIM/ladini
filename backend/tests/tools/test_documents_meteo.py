import sys
sys.path.insert(0, "backend/src")

import os
from typing import List

import agriconnect.services.data_collection.weather.documents_meteo as dm


class MockResponse:
    def __init__(self, text="", headers=None, content=b""):
        self.text = text
        self.headers = headers or {}
        self.content = content

    def raise_for_status(self):
        return None


def test_fetch_index_links(monkeypatch):
    html = (
        '<html><body>'
        '<a href="/produits/bulletin-agrometeologique-decadaire/2024/report1">Report1</a>'
        '<a href="https://meteoburkina.bf/files/report2.pdf">PDF</a>'
        '<a href="#anchor">Anchor</a>'
        '<a href="/short">Short</a>'
        '</body></html>'
    )

    monkeypatch.setattr(dm.requests, "get", lambda url, timeout=15: MockResponse(text=html))

    s = dm.DocumentScraper(index_path="produits/bulletin-agrometeologique-decadaire", base_url="https://meteoburkina.bf")
    links = s._fetch_index_links()

    # Should include the PDF and the longer report path, exclude anchors and short paths
    assert any("report2.pdf" in l for l in links)
    assert any("report1" in l for l in links)
    assert not any(l.endswith("#") for l in links)


def test_scrape_document_content_html(monkeypatch):
    body = (
        '<html><head><title>Mon Titre</title></head>'
        '<body><p>Courte</p><p>' + ('X' * 30) + '</p></body></html>'
    )
    resp = MockResponse(text=body, headers={"Content-Type": "text/html; charset=utf-8"})
    monkeypatch.setattr(dm.requests, "get", lambda url, timeout=15: resp)

    s = dm.DocumentScraper()
    out = s._scrape_document_content("https://example.com/page.html")
    assert out["type"] == "html"
    assert out["title"] == "Mon Titre"
    assert len(out["content"]) > 0


def test_scrape_document_content_pdf(monkeypatch):
    # Prepare fake PDF content and fake PdfReader
    fake_pdf_bytes = b"%PDF-1.4..."
    resp = MockResponse(content=fake_pdf_bytes, headers={"Content-Type": "application/pdf"})
    monkeypatch.setattr(dm.requests, "get", lambda url, timeout=15: resp)

    class FakePage:
        def __init__(self, text):
            self._text = text
        def extract_text(self):
            return self._text

    class FakePdfReader:
        def __init__(self, fp):
            # emulate pages attribute
            self.pages = [FakePage("Texte page 1"), FakePage("Texte page 2")]

    # Ensure module uses our fake PdfReader and reports HAS_PYPDF2
    monkeypatch.setattr(dm, "HAS_PYPDF2", True)
    monkeypatch.setattr(dm, "PdfReader", FakePdfReader)

    s = dm.DocumentScraper()
    out = s._scrape_document_content("https://example.com/report.pdf")

    assert out["type"] == "pdf"
    assert "Texte page 1" in out["content"]
