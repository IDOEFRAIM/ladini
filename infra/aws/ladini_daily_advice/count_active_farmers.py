import psycopg2
import os
from psycopg2.extras import RealDictCursor

host='db-postgresql-fra1-38999-do-user-31802282-0.a.db.ondigitalocean.com'
port=25060
dbname='defaultdb'
user='doadmin'
password='AVNS_-TtxFZrkDLQSQ2W8UiX'

sql = "SELECT count(*) as c FROM auth.users u JOIN auth.user_cultures uc ON uc.user_id = u.id WHERE u.phone IS NOT NULL AND uc.status='active'"

conn = None
try:
    conn = psycopg2.connect(host=host, port=port, user=user, password=password, dbname=dbname, sslmode='require')
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(sql)
    r = cur.fetchone()
    print('count:', r['c'])
    # also show distribution of statuses and sample rows
    cur.execute("SELECT status, count(*) FROM auth.user_cultures GROUP BY status")
    print('\nuser_cultures status distribution:')
    for r in cur.fetchall():
        print(r)
    cur.execute("SELECT id, user_id, culture_name, status, planting_date FROM auth.user_cultures LIMIT 20")
    print('\nSample user_cultures rows:')
    for row in cur.fetchall():
        print(row)
except Exception as e:
    print('error:', e)
finally:
    if conn:
        conn.close()
