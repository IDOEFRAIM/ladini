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

sql = (
    "SELECT u.id, u.phone, u.latitude as lat, u.longitude as lon, uc.culture_name as crop "
    "FROM auth.users u JOIN auth.user_cultures uc ON uc.user_id = u.id "
    "WHERE u.phone IS NOT NULL AND u.latitude IS NOT NULL AND u.longitude IS NOT NULL "
    "AND uc.status='active' LIMIT 10"
)

conn = None
try:
    conn = psycopg2.connect(host=host, port=port, user=user, password=password, dbname=dbname, sslmode='require')
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(sql)
    rows = cur.fetchall()
    print('Found', len(rows), 'rows')
    for r in rows:
        print(r)
except Exception as e:
    print('Query error:', e)
finally:
    if conn:
        conn.close()
