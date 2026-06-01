import os,sys
import psycopg2
pwd = os.environ.get('PGPASSWORD') or input("DB password: ")
try:
    conn = psycopg2.connect(host='127.0.0.1', port=5433, user='ladiniadmin', dbname='postgres', password=pwd)
    print('OK connected')
    conn.close()
except Exception as e:
    print('ERROR', e)
    sys.exit(1)
