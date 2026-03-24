"""Ensure DB prerequisites: create role agriconnect and enable uuid-ossp extension.
This script uses the project's settings.DATABASE_URL and should be run with the project venv python.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agriconnect.core.settings import settings
import psycopg2

DB_URL = settings.DATABASE_URL
if not DB_URL:
    print('ERROR: DATABASE_URL not configured')
    sys.exit(1)

print('Connecting to DB...')
conn = psycopg2.connect(DB_URL)
conn.autocommit = True
cur = conn.cursor()

# 1) Create role if missing
try:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'agriconnect'")
    if cur.fetchone():
        print('Role agriconnect already exists')
    else:
        print('Creating role agriconnect...')
        cur.execute("CREATE ROLE agriconnect WITH LOGIN PASSWORD 'agriconnect_pass'")
        print('Role agriconnect created')
except Exception as e:
    print('Error creating/checking role agriconnect:', e)

# 2) Enable uuid-ossp extension
try:
    print('Enabling extension uuid-ossp (if permitted)...')
    cur.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    print('Extension uuid-ossp ensured')
except Exception as e:
    print('Error creating extension uuid-ossp:', e)

except Exception as e:
    print('Error creating extension uuid-ossp:', e)

# 3) Enable pgcrypto for gen_random_uuid()
try:
    print('Enabling extension pgcrypto (if permitted)...')
    cur.execute('CREATE EXTENSION IF NOT EXISTS pgcrypto')
    print('Extension pgcrypto ensured')
except Exception as e:
    print('Error creating extension pgcrypto:', e)

cur.close()
conn.close()
print('Done.')
