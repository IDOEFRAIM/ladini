import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from agriconnect.services.scraper.scrapers.news_scraper import NewsScraper


BULLETIN_URLS = [
    "https://meteoburkina.bf/produits/bulletin-agrometeologique-decadaire/bulletin-agrom%C3%A9t%C3%A9orologique-d%C3%A9cadaire-n9-valable-du-1er-au-10-avril-2026/",
    "https://meteoburkina.bf/produits/bulletin-agrometeologique-decadaire/bulletin-agrom%C3%A9t%C3%A9orologique-d%C3%A9cadaire-n8-valable-du-21-au-31-mars-2026/",
]


def main() -> int:
    scraper = NewsScraper(config={"max_depth": 1, "max_pages": 20, "respect_robots": False})
    results = []
    for url in BULLETIN_URLS:
        resolved, meta = scraper._resolve_direct_pdf_url(url, max_hops=2)
        results.append({"input_url": url, "resolved_pdf_url": resolved, "meta": meta})

    out_json = ROOT / "tmp" / "contract_check_resolver_output.json"
    out_ndjson = ROOT / "tmp" / "contract_check_ingest_input.ndjson"
    out_json.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = []
    for row in results:
        if row.get("resolved_pdf_url"):
            lines.append(json.dumps({
                "source_id": "meteo_agromet_decadaire",
                "url": row["resolved_pdf_url"],
                "publication_date": "2026-04-11T00:00:00Z"
            }, ensure_ascii=False))
    out_ndjson.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    print(json.dumps({
        "resolver_output": str(out_json),
        "ingest_input": str(out_ndjson),
        "resolved_count": len(lines)
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
