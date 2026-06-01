import psycopg2
from pprint import pprint
conn=psycopg2.connect(host='127.0.0.1',port=5433,dbname='postgres',user='ladiniadmin',password='kingradene',sslmode='require')
cur=conn.cursor()
cur.execute('SELECT COUNT(*) FROM public.pdf_discovery_library')
total=cur.fetchone()[0]
cur.execute("SELECT status, COUNT(*) FROM public.pdf_discovery_library GROUP BY status ORDER BY status")
status_counts=cur.fetchall()
cur.execute("SELECT COUNT(*) FROM public.pdf_discovery_library WHERE status='uploaded'")
uploaded=cur.fetchone()[0]
cur.execute("SELECT COUNT(DISTINCT domain) FROM public.pdf_discovery_library")
domains=cur.fetchone()[0]
cur.close(); conn.close()
print('TOTAL', total)
print('STATUS_COUNTS')
pprint(status_counts)
print('UPLOADED', uploaded)
print('DOMAINS', domains)
