#!/usr/bin/env python3
import traceback
import json
import sys
from pathlib import Path

p = Path('scripts/redis_probe_verbose.py')
OUT = {'file': str(p)}
try:
    text = p.read_text(encoding='utf-8')
except Exception as e:
    OUT['read_error'] = str(e)
    print(json.dumps(OUT, ensure_ascii=False))
    sys.exit(2)

try:
    compile(text, str(p), 'exec')
    OUT['compile'] = 'ok'
except Exception:
    OUT['compile_error'] = traceback.format_exc()

print(json.dumps(OUT, ensure_ascii=False))
