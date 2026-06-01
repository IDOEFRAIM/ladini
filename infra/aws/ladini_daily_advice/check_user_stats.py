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
    cur = conn.cursor()
    queries = [
        ("auth.users - phones not null", "SELECT count(*) FROM auth.users WHERE phone IS NOT NULL"),
        ("auth.users - phones and coords not null", "SELECT count(*) FROM auth.users WHERE phone IS NOT NULL AND latitude IS NOT NULL AND longitude IS NOT NULL"),
        ("public.users - phones not null", "SELECT count(*) FROM public.users WHERE phone IS NOT NULL"),
        ("public.users - phones and coords not null", "SELECT count(*) FROM public.users WHERE phone IS NOT NULL AND latitude IS NOT NULL AND longitude IS NOT NULL"),
    ]
    for label, q in queries:
        cur.execute(q)
        r = cur.fetchone()
        print(label + ":", r[0])

    # show sample users with phone not null
    cur.execute("SELECT id, phone, latitude, longitude FROM auth.users WHERE phone IS NOT NULL LIMIT 5")
    rows = cur.fetchall()
    print('\nauth.users sample with phone not null:')
    for r in rows:
        print(r)

    cur.execute("SELECT id, phone, latitude, longitude FROM public.users WHERE phone IS NOT NULL LIMIT 5")
    rows = cur.fetchall()
    print('\npublic.users sample with phone not null:')
    for r in rows:
        print(r)

except Exception as e:
    print('Error:', e)
finally:
    if conn:
        conn.close()
