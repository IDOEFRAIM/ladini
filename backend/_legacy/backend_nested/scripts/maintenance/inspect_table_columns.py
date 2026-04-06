import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agriconnect.core.settings import settings
import psycopg2

DB = settings.DATABASE_URL
if not DB:
    print('ERROR: DATABASE_URL not set in settings')
    sys.exit(1)

q = '''
SELECT table_schema, table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE (table_schema = 'marketplace' AND table_name IN ('products','stocks'))
ORDER BY table_name, ordinal_position;
'''

conn = psycopg2.connect(DB)
cur = conn.cursor()
cur.execute(q)
rows = cur.fetchall()
for r in rows:
    print('|'.join(map(str, r)))

# sample a few rows
for t in ('marketplace.products','marketplace.stocks'):
    try:
        cur.execute(f'SELECT * FROM {t} LIMIT 3')
        print('\n--- SAMPLE', t)
        for row in cur.fetchall():
            print(row)
    except Exception as e:
        print('SAMPLE ERROR', t, e)

cur.close()
conn.close()
print('\nDone')
