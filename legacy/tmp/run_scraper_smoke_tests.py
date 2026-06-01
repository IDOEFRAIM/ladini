import sys, os, time
root = os.path.join(os.getcwd(), 'backend', 'src')
if root not in sys.path:
    sys.path.insert(0, root)

from agriconnect.services.scraper.scrapers.registry import ScraperRegistry

# Discover scrapers
ScraperRegistry.discover(package_name='agriconnect.services.scraper.scrapers')
keys = ScraperRegistry.list_scrapers()
print('Discovered scrapers:', keys)

# Seeds to try (ordered)
SEEDS = [
    'https://example.com',
    'https://www.fao.org/newsroom/en/',
]

# Scrapers to skip (known to need credentials or non-network context)
SKIP_KEYS = {'google', 'google_workspace', 'gdrive', 'gdoc', 'gform'}

results = {}
for key in keys:
    key_norm = key.strip().lower()
    print('\n--- Testing scraper:', key_norm)
    if key_norm in SKIP_KEYS:
        print('SKIPPED (requires Google auth or special handling)')
        results[key_norm] = {'skipped': True, 'reason': 'requires_credentials'}
        continue

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
        scraper = ScraperRegistry.create(key_norm, config=cfg)
    except Exception as e:
        print('CREATE FAILED:', e)
        results[key_norm] = {'created': False, 'error': str(e)}
        continue

    success = False
    last_err = None
    for seed in SEEDS:
        try:
            print('  trying seed', seed)
            doc, log = scraper.run(seed)
            ok = getattr(log, 'success', False) if log is not None else False
            status = getattr(log, 'http_status', None) if log is not None else None
            print(f'    -> status={status}, success={ok}, title={getattr(doc, "title", None) if doc else None}')
            results[key_norm] = {'created': True, 'seed': seed, 'http_status': status, 'success': bool(ok), 'title': getattr(doc, 'title', None) if doc else None}
            success = True
            break
        except Exception as e:
            print('    ERROR on seed', seed, ':', repr(e))
            last_err = repr(e)
            # brief pause between seeds
            time.sleep(0.2)
    if not success:
        results[key_norm] = {'created': True, 'success': False, 'error': last_err}

print('\n=== SUMMARY ===')
for k, v in results.items():
    print(k, ':', v)

# Exit non-zero if any non-skipped scraper failed
failed = [k for k,v in results.items() if not v.get('skipped') and not v.get('success')]
print('\nFailed scrapers count:', len(failed))
if failed:
    raise SystemExit(2)
