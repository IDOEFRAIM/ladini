import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "backend" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agriconnect.services.scraper.scrapers.news_scraper import NewsScraper

out = ROOT / "tmp" / "quick_scraper_check.ndjson"
if out.exists():
    out.unlink()

cfg = {
    "discovery_output_path": str(out),
    "max_depth": 1,
    "max_pages": 40,
    "deep_extraction": True,
    "network_params": {
        "respect_robots": False,
        "timeout": 12,
    },
}

seed = "https://meteoburkina.bf/produits/bulletin-agrometeologique-decadaire/"
s = NewsScraper(config=cfg)
doc, meta = s.scrape(seed)

records = []
if out.exists():
    with out.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))

pdf_urls = [r.get("pdf_url") for r in records if isinstance(r, dict)]
direct_pdf = [u for u in pdf_urls if isinstance(u, str) and ".pdf" in u.lower()]

result = {
    "seed": seed,
    "discovery_count_meta": meta.get("discovery_count") if isinstance(meta, dict) else None,
    "records_count": len(records),
    "direct_pdf_count": len(direct_pdf),
    "sample_pdf_urls": direct_pdf[:5],
    "output_file": str(out),
}
print(json.dumps(result, ensure_ascii=False, indent=2))
