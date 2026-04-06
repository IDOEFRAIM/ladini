import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agriconnect.core.settings import settings
import psycopg2

PRODUCER_ID = 'fa987f63-fafa-4147-9676-52c9af0edc75'
DB = settings.DATABASE_URL
if not DB:
    print('ERROR: DATABASE_URL not set')
    sys.exit(1)

conn = psycopg2.connect(DB)
cur = conn.cursor()
cur.execute("SELECT id, name, price, quantity_for_sale FROM marketplace.products WHERE producer_id = %s LIMIT 10", (PRODUCER_ID,))
rows = cur.fetchall()
if not rows:
    print('NO_PRODUCTS')
else:
    for r in rows:
        print('PRODUCT', r)
cur.close()
conn.close()
