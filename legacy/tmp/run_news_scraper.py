import json
from agriconnect.services.scraper.scrapers.news_scraper import NewsScraper

targets = [
    ("fao_technical_resources","https://www.fao.org/newsroom/en/","tmp/discovery_fao.ndjson"),
    ("meteo_agromet_decadaire","https://meteoburkina.bf/produits/bulletin-agrometeologique-decadaire/","tmp/discovery_meteo.ndjson"),
]

for source_id, url, out in targets:
    print(f"Running scraper for {source_id} -> {url} (out: {out})")
    s = NewsScraper(config={"discovery_output_path": out})
    doc, meta = s.scrape(url)
    print("Scrape meta:", meta)
    # read ndjson if exists
    try:
        with open(out, 'r', encoding='utf-8') as f:
            print(f"Discovery file {out} content:")
            for line in f:
                try:
                    rec = json.loads(line)
                    print(json.dumps(rec, ensure_ascii=False))
                except Exception as e:
                    print("-- invalid line", e)
    except FileNotFoundError:
        print(f"No discovery file written: {out}")
    print('\n')
