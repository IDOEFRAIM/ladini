from agriconnect.core.settings import settings
import psycopg2
from pprint import pprint

if not settings.DATABASE_URL:
    print('DATABASE_URL not configured')
    raise SystemExit(1)

conn = psycopg2.connect(settings.DATABASE_URL)
cur = conn.cursor()
cur.execute("""
SELECT table_schema, table_name, column_name
FROM information_schema.columns
WHERE table_name = 'users'
ORDER BY table_schema, table_name, ordinal_position
""")
rows = cur.fetchall()
if not rows:
    print('No table named users found in information_schema.columns')
else:
    print('Columns for tables named users:')
    pprint(rows)

cur.close()
conn.close()
