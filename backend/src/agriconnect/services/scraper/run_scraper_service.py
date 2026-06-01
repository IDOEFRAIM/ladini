from __future__ import annotations

import logging
import os
import time

from agriconnect.services.scraper.scraper_orchestrator import ScraperOrchestrator


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scraper_service")


def run_once() -> None:
    orchestrator = ScraperOrchestrator()
    for result in orchestrator.run_all():
        if result.status == "ERROR":
            logger.error("Scraper source failed source_id=%s error=%s", result.source_id, result.error)
        else:
            logger.info(
                "Scraper source completed source_id=%s discovered_count=%s",
                result.source_id,
                int(result.discovered_count or 0),
            )


def main() -> None:
    run_mode = (os.getenv("SCRAPER_RUN_MODE", "once") or "once").strip().lower()
    loop_seconds = int(os.getenv("SCRAPER_LOOP_SECONDS", "900"))

    if run_mode == "loop":
        logger.info("Starting scraper service in loop mode (interval=%ss)", loop_seconds)
        while True:
            run_once()
            time.sleep(max(loop_seconds, 5))
    else:
        logger.info("Starting scraper service in one-shot mode")
        run_once()


if __name__ == "__main__":
    main()
