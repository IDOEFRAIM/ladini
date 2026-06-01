import sys, glob, importlib, traceback, os
sys.path.insert(0, os.getcwd())
files = glob.glob('backend/src/agriconnect/services/scraper/scrapers/*.py')
print('FOUND', len(files))
errs = 0
for f in sorted(files):
    mod = f.replace('\\\\','/').replace('.py','').replace('/','.')
    try:
        importlib.import_module(mod)
        print('OK', mod)
    except Exception as e:
        errs += 1
        print('ERR', mod, ':', e)
        traceback.print_exc()

print('\nSUMMARY: total=%d, errors=%d' % (len(files), errs))
if errs:
    raise SystemExit(1)
