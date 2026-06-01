import sys, os
root = os.path.join(os.getcwd(), 'backend', 'src')
if root not in sys.path:
    sys.path.insert(0, root)

from agriconnect.services.scraper.scrapers.fao_doi_resolver import FaoDoiResolver

s = FaoDoiResolver(config={'network_params': {'respect_robots': False, 'timeout': 20, 'max_attempts': 1}})

doc, meta = s.scrape('https://www.fao.org/3/cc6683en/cc6683en.pdf')
print('doc is None?', doc is None)
print('meta keys:', list(meta.keys()))
print('http_status', meta.get('http_status'))
print('bytes', meta.get('bytes_downloaded'))
raw = meta.get('raw_payload')
print('raw_preview:', (raw[:200] if isinstance(raw, str) else str(raw)) )
if doc:
    print('doc.title=', doc.title)
    print('doc.metadata keys=', list(doc.metadata.keys()))
    print('doc.md len=', len(doc.content_markdown or ''))
