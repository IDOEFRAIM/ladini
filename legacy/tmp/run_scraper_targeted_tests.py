import sys, os, time, json
root = os.path.join(os.getcwd(), 'backend', 'src')
if root not in sys.path:
    sys.path.insert(0, root)

from agriconnect.services.scraper.scrapers.registry import ScraperRegistry

ScraperRegistry.discover(package_name='agriconnect.services.scraper.scrapers')
keys = ScraperRegistry.list_scrapers()
print('Discovered scrapers:', keys)

# Targeted seeds for non-conclusive scrapers
SEEDS_MAP = {
    'crawler': ['https://www.fao.org/newsroom/en/'],
    'data_platform': ['https://data.humdata.org/'],
    'dataset_catalog': ['https://data.un.org/'],
    'doi': ['https://doi.org/10.3897/zookeys.100.200'],
    'fao': ['https://www.fao.org/publications/en/'],
    'fao_doi': ['https://www.fao.org/3/cc6683en/cc6683en.pdf'],
    'pdf': ['https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf'],
    'pdf_document': ['https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf'],
    'statistics': ['https://www.fao.org/faostat/en/'],
    'technical': ['https://www.fao.org/technical-resources/en/'],
    'technical_crawler': ['https://www.fao.org/technical-resources/en/'],
    'technical_site': ['https://www.fao.org/technical-resources/en/'],
}

# Skip scrapers that require Google auth
SKIP_KEYS = {'gdoc', 'gdrive', 'gform', 'google', 'google_workspace'}

results = {}
for key in keys:
    kn = key.strip().lower()
    if kn in SKIP_KEYS:
        results[kn] = {'skipped': True}
        continue
    seeds = SEEDS_MAP.get(kn, ['https://example.com'])
    cfg = {
        'network_params': {
            'timeout': 8,
            'min_delay_s': 0,
            'max_delay_s': 0.2,
            'max_attempts': 1,
            'respect_robots': False,
        }
    }
    try:
        scraper = ScraperRegistry.create(kn, config=cfg)
    except Exception as e:
        results[kn] = {'created': False, 'error': str(e)}
        continue
    success = False
    last_err = None
    for seed in seeds:
        try:
            print(f'Testing {kn} -> {seed}')
            doc, log = scraper.run(seed)
            ok = getattr(log, 'success', False) if log is not None else False
            status = getattr(log, 'http_status', None) if log is not None else None
            title = getattr(doc, 'title', None) if doc else None
            results[kn] = {'created': True, 'seed': seed, 'http_status': status, 'success': bool(ok), 'title': title}
            print('  result:', results[kn])
            success = True
            break
        except Exception as e:
            print(f'  error for {kn} on {seed}:', repr(e))
            last_err = repr(e)
            time.sleep(0.2)
    if not success:
        results[kn] = {'created': True, 'success': False, 'error': last_err}

out_path = 'tmp/scraper_targeted_results.json'
with open(out_path, 'w', encoding='utf-8') as fh:
    json.dump(results, fh, indent=2, ensure_ascii=False)

print('\nSaved results to', out_path)
print('Failed count:', len([k for k,v in results.items() if not v.get('skipped') and not v.get('success')]))
