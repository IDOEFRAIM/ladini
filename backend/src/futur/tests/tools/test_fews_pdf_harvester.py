import json
import os
from pathlib import Path

import pytest

import importlib.util
from pathlib import Path

# Load module from source path to avoid package import issues in some test runs
fpath = Path(__file__).resolve().parents[2] / "src" / "agriconnect" / "services" / "data_collection" / "documents" / "fews_pdf_harvester.py"
spec = importlib.util.spec_from_file_location("fews_pdf_harvester", str(fpath))
harvester_mod = importlib.util.module_from_spec(spec)
import sys
import types

# Provide a minimal stub for pdfplumber to avoid heavy dependency during import
pdfplumber_stub = types.ModuleType("pdfplumber")
def _fake_open(path):
    class DummyPDF:
        pages = []
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
    return DummyPDF()
pdfplumber_stub.open = _fake_open
sys.modules.setdefault("pdfplumber", pdfplumber_stub)

spec.loader.exec_module(harvester_mod)


class MockResp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if not (200 <= self.status_code < 300):
            raise Exception(f"HTTP {self.status_code}")


def test_get_report_pages_parsing(monkeypatch):
    search_html = (
        '<html><body>'
        '<a href="/burkina-faso/mise-jour/report-one">Report One Long Title</a>'
        '<a href="/files/doc1.pdf">Download doc</a>'
        '<a href="/other/short">x</a>'
        '</body></html>'
    )

    def mock_get(url, headers=None, timeout=None):
        return MockResp(search_html, 200)

    monkeypatch.setattr(harvester_mod.requests, "get", mock_get)

    scraper = harvester_mod.AnamBulletinScraper()
    reports = scraper.get_report_pages()

    # Expect two valid entries: one PDF and one report page
    urls = {r["url"] for r in reports}
    assert any('.pdf' in u for u in urls)
    assert any('/burkina-faso/' in u for u in urls)


def test_run_creates_files(monkeypatch, tmp_path):
    # Prepare a fake reports list: one direct PDF and one that needs find via print
    reports = [
        {"title": "Direct PDF", "url": "https://fews.net/files/doc1.pdf", "direct_pdf": True},
        {"title": "Report Page", "url": "https://fews.net/burkina-faso/report-two", "direct_pdf": False},
    ]

    # monkeypatch network and pdf behaviors
    monkeypatch.setattr(harvester_mod.AnamBulletinScraper, "get_report_pages", lambda self: reports)
    monkeypatch.setattr(harvester_mod.AnamBulletinScraper, "find_pdf_on_print_page", lambda self, url: None)

    # _find_download_button should return a pdf for the report page
    monkeypatch.setattr(harvester_mod.AnamBulletinScraper, "_find_download_button", lambda self, url: "https://fews.net/files/doc2.pdf")

    # replace download_file to actually create a fake pdf file in tmp_path
    def fake_download(self, url, title):
        fname = (title.replace(' ', '_')[:50] + ".pdf")
        p = tmp_path / fname
        p.write_bytes(b"%PDF-1.4\n%fakepdf\n")
        return str(p), False

    monkeypatch.setattr(harvester_mod.AnamBulletinScraper, "download_file", fake_download)

    # patch extract_text_from_pdf to return a long text
    monkeypatch.setattr(harvester_mod.AnamBulletinScraper, "extract_text_from_pdf", lambda self, p: "X" * 600)

    # Redirect module-level dirs into tmp_path
    monkeypatch.setattr(harvester_mod, "PDF_DIR", str(tmp_path / "pdfs"))
    monkeypatch.setattr(harvester_mod, "TEXT_DIR", str(tmp_path / "texts"))
    monkeypatch.setattr(harvester_mod, "METADATA_DIR", str(tmp_path / "meta"))
    os.makedirs(harvester_mod.PDF_DIR, exist_ok=True)
    os.makedirs(harvester_mod.TEXT_DIR, exist_ok=True)
    os.makedirs(harvester_mod.METADATA_DIR, exist_ok=True)

    scraper = harvester_mod.AnamBulletinScraper()
    scraper.run()

    # Check that metadata files and text files were created
    meta_files = list(Path(harvester_mod.METADATA_DIR).glob("*.json"))
    text_files = list(Path(harvester_mod.TEXT_DIR).glob("*.txt"))

    assert len(meta_files) >= 1
    assert len(text_files) >= 1

    # Validate metadata structure
    with meta_files[0].open(encoding="utf-8") as fh:
        meta = json.load(fh)
    assert "title" in meta and "pdf_url" in meta and "pdf_path" in meta
