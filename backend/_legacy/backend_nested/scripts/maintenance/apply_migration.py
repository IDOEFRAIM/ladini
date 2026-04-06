from agriconnect.core.settings import settings
import psycopg2
import sys
from pathlib import Path

sql_file = Path(__file__).resolve().parents[1] / 'scripts' / 'ensure_users_columns.sql'
print('SQL file:', sql_file)
print('Using DATABASE_URL configured? ', bool(settings.DATABASE_URL))
if not settings.DATABASE_URL:
    print('ERROR: DATABASE_URL not set in settings')
    sys.exit(1)

with open(sql_file, 'r', encoding='utf-8') as f:
    sql = f.read()

try:
    conn = psycopg2.connect(settings.DATABASE_URL)
    cur = conn.cursor()
    cur.execute(sql)
    conn.commit()
    print('Migration applied successfully')
except Exception as e:
    print('Migration failed:', repr(e))
    try:
        conn.rollback()
    except Exception:
        pass
    sys.exit(2)
finally:
    try:
        cur.close()
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass
