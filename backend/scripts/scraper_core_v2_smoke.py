from __future__ import annotations

import json
from pathlib import Path

from agriconnect.core.persister import FilePersister
from agriconnect.core.schemas import RawDocument, ScraperLog
from agriconnect.core.scraper_checkpoint import CheckpointManager
from agriconnect.core.scraper_config import get_config


def main() -> int:
    cfg = get_config()

    cp_dir = Path("backend/tmp/checkpoints_smoke")
    cp = CheckpointManager(checkpoint_dir=cp_dir)
    state = cp.load_or_create(
        "smoke_v2",
        {
            "news_and_articles": ["https://example.com/article"],
        },
    )

    doc = RawDocument(
        id="smoke_doc_001",
        url="https://example.com/article",
        title="Smoke Document",
        content_markdown="# Smoke\n\ncontent",
        metadata={"source": "smoke"},
        language="fr",
    )
    log = ScraperLog(
        scraper_name="news",
        version="2.0.0",
        http_status=200,
        duration_ms=12,
        bytes_downloaded=1234,
        content_density=0.45,
        success=True,
    )

    persister = FilePersister(output_dir="backend/tmp/persister_smoke")
    saved_path = persister.save(doc, log, category="news_and_articles")

    output = {
        "config_class": cfg.__class__.__name__,
        "checkpoint_session": state.session_id,
        "persisted_path": saved_path,
        "checkpoint_progress": cp.get_progress(),
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
