# AgriConnect Scraper Engine V2

## Overview
This directory contains the second-generation (V2) scraper engine used by Ladini.
The design principle is "Zero-IO": scrapers fetch and parse remote resources entirely in memory
and emit canonical Python objects for downstream consumers. This keeps scraping separate from
storage, ingestion, or indexing responsibilities.

Key points:
- Scrapers never write files to disk or upload directly to S3.
- Scrapers return typed objects (`RawDocument`, `ScraperLog`) that the orchestrator or ingestion
	layer can persist or transform.

## Core Contracts
- `agriconnect.core.schemas.RawDocument`: canonical in-memory document produced by scrapers.
- `agriconnect.core.schemas.ScraperLog`: monitoring sidecar recording status, bytes, timing, and errors.

Contract: every scraper implements a `run(url: str)` method that returns a tuple:
```
(Optional[RawDocument], ScraperLog)
```

## How the Scraping System is Structured

- `scrapers/base.py` — The abstract base class describing the scraper contract. It centralizes
	HTTP session creation, polite fetching (throttling / robots.txt), and the `run()` wrapper that
	calls the concrete `scrape(url)` implementation. Helpers for cleaning HTML and building
	`RawDocument` instances live here.

- `scrapers/registry.py` — Lightweight plugin registry. Use the `@register_scraper("key")`
	decorator to register scraper implementations. Create scrapers at runtime using
	`ScraperRegistry.create(key, config=...)`.

- `scrapers/news_scraper.py` — Example HTML scraper that extracts article content and converts
	it to markdown while preserving links and basic structure.

- `scrapers/pdf_downloader.py` — Memory-only PDF extraction engine. Supports a fast text-only
	mode based on `pypdf` and a higher-fidelity mode using `pdfplumber` for table extraction.

- `scrapers/institutional_pdf_harvester.py` — Generic engine for institutional websites that
	list PDF reports (e.g. bulletins). It discovers PDF links on listing pages and aggregates
	parsed content into a single `RawDocument`.

- `scraper_orchestrator.py` — YAML-driven orchestrator that reads `backend/sources/sources.yaml`,
	instantiates scrapers with per-source `config`, runs them, and enriches returned documents with
	source metadata.

## Registry usage

Register a scraper implementation:

```python
from agriconnect.services.scraper.scrapers.registry import register_scraper

@register_scraper("news")
class NewsScraper(BaseScraper):
		...
```

Create and run a scraper dynamically:

```python
from agriconnect.services.scraper.scraper_orchestrator import ScraperOrchestrator

orch = ScraperOrchestrator()
result = orch.run_source("lefaso_actualites")
if result.status == "SUCCESS":
		doc = result.document
		log = result.log
```

## Best practices for contributors

- Keep scrapers focused: they must only fetch and parse; do not add persistence logic.
- Use `self.config` in your scraper `__init__` to make behaviour configurable by `sources.yaml`.
- Prefer small, well-tested helpers in `scraper_utils.py` instead of copying logic into scrapers.

## Quick local smoke test

Run the following to ensure the orchestrator can load `sources.yaml` and instantiate scrapers:

```powershell
python -c "import sys; sys.path.insert(0,'backend/src'); from agriconnect.services.scraper.scraper_orchestrator import ScraperOrchestrator; o=ScraperOrchestrator(); print(o.list_sources())"
```

If you want help adding a new scraper, open a PR with a branch named `feat/scraper-<name>` and
include a short README for the scraper explaining the config keys it supports.

---
Credits: Architecture and refactor by the Ladini data engineering team
