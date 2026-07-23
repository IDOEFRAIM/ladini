import sys
import os
from urllib.parse import urlparse
import importlib.util

# Avoid importing package-level modules that pull heavy optional deps (pdfplumber).
# Load SonagessScraper directly from the source file to prevent import-time side effects.
sys.path.insert(0, "backend/src")
sonagess_path = os.path.join("backend", "src", "agriconnect", "services", "data_collection", "documents", "sonagess.py")
spec = importlib.util.spec_from_file_location("sonagess_mod", sonagess_path)
sonagess_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sonagess_mod)
SonagessScraper = sonagess_mod.SonagessScraper


def test_sonagess_crawl_and_extract(tmp_path):
    outdir = str(tmp_path)
    scraper = SonagessScraper(start_url="https://example.com/start", download=True, output_dir=outdir, max_depth=1, max_pages=10)

    # Prepare synthetic HTML pages
    html_map = {
        "https://example.com/start": '<html><body>'
                                    '<a href="report1.pdf">Report 1</a>'
                                    '<a href="/page2">Page 2</a>'
                                    '</body></html>',
        "https://example.com/page2": '<html><body>'
                                     '<a href="report2.pdf">Report 2</a>'
                                     '</body></html>'
    }

    # Stub network and PDF methods to avoid real HTTP I/O
    scraper._fetch_html = lambda url: html_map.get(url)

    def fake_download(url: str) -> str:
        name = os.path.basename(urlparse(url).path) or "doc.pdf"
        p = tmp_path / name
        p.write_text("PDF-DATA")
        return str(p)

    scraper._download_pdf = fake_download
    scraper._extract_text_from_pdf = lambda path: "EXTRACTED_TEXT"

    # Run crawl and processing
    scraper._crawl()

    # We should have found two PDF urls
    assert any("report1.pdf" in k for k in scraper._found.keys())
    assert any("report2.pdf" in k for k in scraper._found.keys())

    scraper._process_downloads_and_extraction()

    # After processing, all found entries should have content set
    for k, v in scraper._found.items():
        assert v.get("downloaded_path") is not None
        assert v.get("content") == "EXTRACTED_TEXT"
