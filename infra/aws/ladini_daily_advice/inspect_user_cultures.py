import psycopg2
from psycopg2.extras import RealDictCursor
host='db-postgresql-fra1-38999-do-user-31802282-0.a.db.ondigitalocean.com'
port=25060
dbname='defaultdb'
user='doadmin'
password='AVNS_-TtxFZrkDLQSQ2W8UiX'
conn=None
try:
    conn = psycopg2.connect(host=host, port=port, user=user, password=password, dbname=dbname, sslmode='require')
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT status, count(*) FROM auth.user_cultures GROUP BY status")
    for r in cur.fetchall():
        print(r)
    cur.execute("SELECT id, user_id, culture_name, status, planting_date FROM auth.user_cultures LIMIT 20")
    for r in cur.fetchall():
        print(r)
except Exception as e:
    print('error', e)
finally:
    if conn:
        conn.close()
