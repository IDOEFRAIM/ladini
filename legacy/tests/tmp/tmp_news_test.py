from types import SimpleNamespace
from agriconnect.services.scraper.scrapers.news_scraper import NewsScraper

s = NewsScraper()
html = '<html><head><title>Test</title></head><body><a href="http://example.com/report.pdf">PDF Report</a></body></html>'

s._safe_page_get = lambda u: SimpleNamespace(status_code=200, text=html)

records = list(s._discover_recursive('http://example.com/'))
print('RECORDS:', records)

# test scrape writes
import tempfile, os
fd, path = tempfile.mkstemp(prefix='news_disc_', suffix='.ndjson')
os.close(fd)
s.discovery_output_path = path
doc, meta = s.scrape('http://example.com/')
print('SCRAPE_META:', meta)
with open(path, 'r', encoding='utf-8') as fh:
    print('FILE_CONTENTS:\n' + fh.read())
os.remove(path)
