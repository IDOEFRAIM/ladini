import glob
import importlib.util
import traceback

files = glob.glob('backend/src/agriconnect/services/scraper/scrapers/*.py')
print('FOUND', len(files))
errs = 0
for f in sorted(files):
    name = f.replace('\\\\','/').split('/')[-1].rsplit('.', 1)[0]
    try:
        spec = importlib.util.spec_from_file_location(name, f)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        print('OK', f)
    except Exception as e:
        errs += 1
        print('ERR', f, ':', e)
        traceback.print_exc()

print('\nSUMMARY: total=%d, errors=%d' % (len(files), errs))
if errs:
    raise SystemExit(1)
