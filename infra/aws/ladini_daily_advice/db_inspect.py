import os
import psycopg2
from psycopg2.extras import RealDictCursor

# Credentials (same as test)
os.environ['DB_HOST'] = 'db-postgresql-fra1-38999-do-user-31802282-0.a.db.ondigitalocean.com'
os.environ['DB_PORT'] = '25060'
os.environ['DB_NAME'] = 'defaultdb'
os.environ['DB_USER'] = 'doadmin'
os.environ['DB_PASSWORD'] = 'AVNS_-TtxFZrkDLQSQ2W8UiX'

host = os.environ['DB_HOST']
port = int(os.environ['DB_PORT'])
user = os.environ['DB_USER']
dbname = os.environ['DB_NAME']
password = os.environ['DB_PASSWORD']

print('Connecting to', host)
conn = None
try:
    conn = psycopg2.connect(host=host, port=port, user=user, password=password, dbname=dbname, sslmode='require')
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema NOT IN ('information_schema','pg_catalog') ORDER BY table_schema, table_name;")
    tables = cur.fetchall()
    print('Found tables:')
    for t in tables:
        print('-', t['table_schema'] + '.' + t['table_name'])

    # Try to show sample rows from public.users if exists
    cur.execute("SELECT * FROM public.users LIMIT 5;")
    rows = cur.fetchall()
    print('\nSample rows from public.users:')
    for r in rows:
        print(r)

except Exception as e:
    print('Error during DB inspection:', e)
finally:
    if conn:
        conn.close()
