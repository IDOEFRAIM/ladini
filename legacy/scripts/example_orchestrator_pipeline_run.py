"""Example: native pipeline mode with ScraperOrchestrator + PdfDownloader callback.

Run:
    & ".venv\\Scripts\\python.exe" scripts\\example_orchestrator_pipeline_run.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "backend" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agriconnect.services.scraper.scraper_orchestrator import ScraperOrchestrator
from agriconnect.services.scraper.scrapers.pdf_downloader import PdfDownloader


def main() -> None:
    downloader = PdfDownloader(
        config={
            "raw_pdf_dir": str(ROOT / "tmp" / "pipeline_raw_pdfs"),
            "strict_pdf_content_type": True,
        }
    )

    orchestrator = ScraperOrchestrator(
        sources_file=str(ROOT / "backend" / "sources" / "sources.yaml"),
        discovery_dir=str(ROOT / "tmp" / "discovery_pipeline"),
        auto_discover=True,
    )

    # Pipeline mode: each discovery record is ingested immediately.
    results = list(orchestrator.run_all(ingestion_callback=downloader.handle))

    summary = {
        "total_sources": len(results),
        "success_count": sum(1 for r in results if r.status == "SUCCESS"),
        "failure_count": sum(1 for r in results if r.status == "ERROR"),
        "audit": orchestrator.get_audit_report(),
    }

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
