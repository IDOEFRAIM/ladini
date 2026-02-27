import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from agriconnect.services.data_collection.documents.fews import FewsNetScraper


class MockResp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


def test_fetch_success(monkeypatch):
    html = "<html><body>Hello</body></html>"

    def mock_get(url, headers=None, timeout=None):
        return MockResp(html, 200)

    monkeypatch.setattr(
        "agriconnect.services.data_collection.documents.fews.requests.get", mock_get
    )

    scraper = FewsNetScraper()
    content, err = scraper._fetch("http://example.com")
    assert err is None
    assert "Hello" in content


def test_select_clean_extract_and_validate():
    html = "<html><body><main><nav>nav</nav><h1>Title</h1><p>" + ("a" * 300) + "</p></main></body></html>"
    soup = BeautifulSoup(html, "html.parser")
    scraper = FewsNetScraper()

    div = scraper._select_content_div(soup)
    assert div is not None

    # clean and extract
    scraper._clean_content_div(div)
    text = scraper._extract_text_from_div(div)
    assert "nav" not in text
    assert scraper._is_valid_text(text)


def test_extract_deep_content_and_run(monkeypatch, tmp_path):
    index_html = '<html><body><a href="/burkina-faso/mise-jour/test-report">Test Report Title</a></body></html>'
    detail_html = "<html><body><main><p>" + ("b" * 1000) + "</p></main></body></html>"

    def mock_get(url, headers=None, timeout=None):
        # base index
        if url.endswith("/fr/west-africa/burkina-faso") or url.endswith("/burkina-faso"):
            return MockResp(index_html, 200)
        # detail page
        return MockResp(detail_html, 200)

    monkeypatch.setattr(
        "agriconnect.services.data_collection.documents.fews.requests.get", mock_get
    )

    scraper = FewsNetScraper(country_slug="burkina-faso")
    # write into temp dir to avoid touching repo
    scraper.base_dir = str(tmp_path)
    scraper.url = "https://fews.net/fr/west-africa/burkina-faso"

    scraper.run()

    files = list(Path(scraper.base_dir).iterdir())
    json_files = [f for f in files if f.suffix == ".json"]
    assert len(json_files) >= 1

    # validate saved JSON structure
    with json_files[0].open(encoding="utf-8") as fh:
        data = json.load(fh)
    assert "metadata" in data and "content" in data
