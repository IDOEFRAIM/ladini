import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "backend" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agriconnect.services.scraper.scrapers.news_scraper import NewsScraper

cfg = {
    "max_depth": 1,
    "max_pages": 20,
    "deep_extraction": True,
    "network_params": {
        "respect_robots": False,
        "timeout": 12,
    },
}

candidate = "https://meteoburkina.bf/produits/bulletin-agrometeologique-decadaire/bulletin-agrom%C3%A9t%C3%A9orologique-d%C3%A9cadaire-n9-valable-du-1er-au-10-avril-2026/"
s = NewsScraper(config=cfg)
resolved, meta = s._resolve_direct_pdf_url(candidate, max_hops=3)

print(json.dumps({
    "candidate": candidate,
    "resolved": resolved,
    "meta": meta,
    "is_direct_pdf": isinstance(resolved, str) and ".pdf" in resolved.lower(),
}, ensure_ascii=False, indent=2))
