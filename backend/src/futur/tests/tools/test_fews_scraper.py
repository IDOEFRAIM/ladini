from agriconnect.core.schemas import RawDocument
from agriconnect.services.scraper.scrapers.institutional_pdf_harvester import FewsNetScraper


def test_extract_deep_content_uses_service_engine(monkeypatch):
    scraper = FewsNetScraper(country_slug="burkina-faso")

    expected = RawDocument(
        id="test-id",
        url="https://example.com/report",
        title="Test",
        content_markdown="# Report\n\nContent",
        metadata={"source_type": "institutional_document"},
    )

    monkeypatch.setattr(scraper.engine, "run", lambda url: (expected, None))

    out = scraper.extract_deep_content("https://example.com/report")
    assert out is not None
    assert "# Report" in out


def test_run_returns_normalized_result(monkeypatch):
    scraper = FewsNetScraper(country_slug="burkina-faso")
    monkeypatch.setattr(
        scraper.engine,
        "run_harvest",
        lambda start_url=None: {
            "status": "SUCCESS",
            "results": [{"title": "R1", "url": "https://example.com"}],
            "error": None,
        },
    )

    result = scraper.run()
    assert result["status"] == "SUCCESS"
    assert len(result["results"]) == 1
    assert result["error"] is None
