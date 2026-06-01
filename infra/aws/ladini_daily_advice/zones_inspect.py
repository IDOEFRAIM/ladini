import os
import psycopg2
from psycopg2.extras import RealDictCursor

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

conn = None
try:
    conn = psycopg2.connect(host=host, port=port, user=user, password=password, dbname=dbname, sslmode='require')
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='zones'")
    cols = cur.fetchall()
    print('public.zones columns:')
    for c in cols:
        print('-', c['column_name'])

    cur.execute("SELECT * FROM public.zones LIMIT 5")
    rows = cur.fetchall()
    print('\nSample public.zones rows:')
    for r in rows:
        print(r)

except Exception as e:
    print('Error:', e)
finally:
    if conn:
        conn.close()
