import sys
sys.path.insert(0, "backend/src")

from agriconnect.core.schemas import RawDocument
import agriconnect.services.data_collection.weather.documents_meteo as dm


class DummyLog:
    def __init__(self, error_trace=None):
        self.error_trace = error_trace


class DummyEngine:
    def __init__(self, result):
        self._result = result

    def run(self, _url):
        return self._result


class DummyMeteoScraper:
    def __init__(self, *args, **kwargs):
        _ = (args, kwargs)
        self.engine = None


def test_scrape_bulletins_success(monkeypatch):
    doc = RawDocument(
        id="doc-test-1",
        title="Bulletin Agro",
        content_markdown="Contenu bulletin meteo",
        url="https://meteoburkina.bf/bulletin.pdf",
        language="fr",
        metadata={"kind": "bulletin"},
    )

    scraper = DummyMeteoScraper()
    scraper.engine = DummyEngine((doc, DummyLog()))
    monkeypatch.setattr(dm, "MeteoBurkinaScraper", lambda *a, **k: scraper)

    s = dm.DocumentScraper()
    out = s.scrape_bulletins()

    assert out["status"] == "SUCCESS"
    assert len(out["results"]) == 1
    assert out["results"][0]["title"] == "Bulletin Agro"
    assert "Contenu bulletin" in out["results"][0]["content"]


def test_scrape_bulletins_error(monkeypatch):
    scraper = DummyMeteoScraper()
    scraper.engine = DummyEngine((None, DummyLog(error_trace="network_failed")))
    monkeypatch.setattr(dm, "MeteoBurkinaScraper", lambda *a, **k: scraper)

    s = dm.DocumentScraper()
    out = s.scrape_bulletins()

    assert out["status"] == "ERROR"
    assert out["results"] == []
    assert "network_failed" in out["message"]
