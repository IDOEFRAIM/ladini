import urllib.request
import json

try:
    with urllib.request.urlopen('http://localhost:8004/health/ready', timeout=5) as r:
        print(r.read().decode())
except Exception as e:
    print('ERROR', e)
