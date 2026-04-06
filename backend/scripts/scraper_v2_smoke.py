from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _bootstrap_path() -> None:
    root = Path(__file__).resolve().parents[1]
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


def main() -> int:
    _bootstrap_path()

    from agriconnect.services.scraper.scrapers import ScraperRegistry

    parser = argparse.ArgumentParser(description="Scraper Engine V2 smoke test")
    parser.add_argument("--scraper", default="news", help="registry key (news|pdf|technical)")
    parser.add_argument("--url", default="", help="optional URL to execute scrape")
    args = parser.parse_args()

    keys = ScraperRegistry.keys()
    out = {"registered": keys, "selected": args.scraper, "executed": False}

    if args.scraper not in keys:
        out["error"] = f"unknown scraper key: {args.scraper}"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    if not args.url:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    scraper = ScraperRegistry.create(args.scraper, config={"timeout": 20})
    doc, log = scraper.run(args.url)
    out["executed"] = True
    out["success"] = bool(doc is not None)
    out["log"] = log.model_dump()
    if doc is not None:
        out["doc"] = {
            "id": doc.id,
            "title": doc.title,
            "url": str(doc.url),
            "language": doc.language,
            "markdown_preview": doc.content_markdown[:180],
        }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
