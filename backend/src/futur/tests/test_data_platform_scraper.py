from __future__ import annotations

import json
import os
from types import SimpleNamespace

from agriconnect.services.scraper.scrapers.data_platform_scraper import DataPlatformScraper


def make_response(html: str, url: str = "https://example.org/page"):
    class DummyResp(SimpleNamespace):
        pass
    r = DummyResp()
    r.text = html
    r.content = html.encode("utf-8")
    r.status_code = 200
    r.headers = {"Content-Type": "text/html; charset=utf-8"}
    r.url = url
    def json_func():
        raise ValueError("not json")
    r.json = json_func
    return r


def test_data_platform_discovery(tmp_path, monkeypatch):
    html = """
    <html><head><title>Test Dataset</title></head>
    <body>
      <main>
        <a href="https://cdn.example.org/uploads/report.pdf">Download report</a>
      </main>
    </body>
    </html>
    """

    # prepare scraper with discovery output path in tmp
    ndjson = tmp_path / "data_platform.ndjson"
    cfg = {"discovery_output_path": str(ndjson)}
    scraper = DataPlatformScraper(config=cfg)

    # monkeypatch _request to return our static HTML
    monkeypatch.setattr(scraper, "_request", lambda url: make_response(html, url=url))
    # monkeypatch the function used by the scraper module (it was imported at module scope)
    import agriconnect.services.scraper.scrapers.data_platform_scraper as dps
    monkeypatch.setattr(dps, "extract_markdown_from_html", lambda html, include_links=True: "# Dummy\n\nExtracted content")

    # monkeypatch HEAD requests to the PDF to avoid external network calls
    def make_head_resp(url):
        from types import SimpleNamespace
        r = SimpleNamespace()
        r.status_code = 200
        r.headers = {"Content-Type": "application/pdf", "Content-Length": "12345"}
        r.url = url
        return r

    monkeypatch.setattr(scraper.session, "head", lambda url, allow_redirects=True, timeout=None: make_head_resp(url))

    doc, meta = scraper.scrape("https://example.org/dataset/1")

    assert doc is not None
    # discovery file must exist and include the PDF url
    assert ndjson.exists()
    with ndjson.open("r", encoding="utf-8") as fh:
        lines = [json.loads(l) for l in fh if l.strip()]
    urls = [l.get("pdf_url") for l in lines]
    assert any("/report.pdf" in (u or "") for u in urls)
