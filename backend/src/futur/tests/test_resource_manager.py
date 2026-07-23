import sys
import json
from pathlib import Path

sys.path.insert(0, "backend/src")

from agriconnect.services.scraper.core.resource_manager import ResourceManager


class FakeScraperSuccess:
    def __init__(self, output_dir: str = None):
        self.output_dir = output_dir

    def scrape(self, url: str):
        return {"status": "success", "url": url, "title": "OK", "content": "data"}


class FakeScraperFail:
    def __init__(self, output_dir: str = None):
        self.output_dir = output_dir

    def scrape(self, url: str):
        return {"status": "failed", "url": url, "error": "not found"}


def test_process_sources_success_and_failure(tmp_path):
    mgr = ResourceManager(output_dir=str(tmp_path))

    # Use a temp catalog file so we don't write into the repo
    mgr.catalog_file = tmp_path / "catalog.json"
    mgr.catalog = []

    sources = {
        "success_cat": ["https://example.com/1", "https://example.com/2"],
        "fail_cat": ["https://example.com/x"]
    }

    # Provide class for success (should be instantiated) and instance for fail
    scrapers_map = {
        "success_cat": FakeScraperSuccess,
        "fail_cat": FakeScraperFail()
    }

    stats = mgr.process_sources(sources, scrapers_map)

    # Check stats
    assert stats["total_sources"] == 3
    assert stats["successful"] == 2
    assert stats["failed"] == 1

    # Catalog should contain two successful entries
    assert len(mgr.catalog) == 2
    urls = {r.get("url") for r in mgr.catalog}
    assert "https://example.com/1" in urls
    assert "https://example.com/2" in urls

    # Catalog file should have been written
    assert mgr.catalog_file.exists()
    data = json.loads(mgr.catalog_file.read_text(encoding="utf-8"))
    assert isinstance(data, list)
