import os
import psycopg2
from psycopg2.extras import RealDictCursor

host=os.environ.get('DB_HOST','db-postgresql-fra1-38999-do-user-31802282-0.a.db.ondigitalocean.com')
port=int(os.environ.get('DB_PORT','25060'))
dbname=os.environ.get('DB_NAME','defaultdb')
user=os.environ.get('DB_USER','doadmin')
password=os.environ.get('DB_PASSWORD','AVNS_-TtxFZrkDLQSQ2W8UiX')
sslmode=os.environ.get('DB_SSLMODE','require')
sslrootcert=os.environ.get('DB_SSLROOTCERT','')

print('Connecting to', host, port, dbname, 'sslmode=', sslmode, 'sslrootcert=', sslrootcert)
conn = None
try:
    conn = psycopg2.connect(host=host, port=port, dbname=dbname, user=user, password=password, sslmode=sslmode)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    sql = """
    SELECT u.id, u.phone, u.latitude, u.longitude, u.zone_id, uc.culture_name, uc.status
    FROM auth.users u
    LEFT JOIN auth.user_cultures uc ON uc.user_id = u.id
    WHERE u.phone IS NOT NULL
      AND (u.latitude IS NOT NULL AND u.longitude IS NOT NULL OR u.zone_id IS NOT NULL)
    LIMIT 100
    """
    cur.execute(sql)
    rows = cur.fetchall()
    print('rows:', len(rows))
    for r in rows:
        print(r)
except Exception as e:
    print('error:', e)
finally:
    if conn:
        conn.close()
