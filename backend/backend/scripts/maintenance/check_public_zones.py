import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agriconnect.core.settings import settings
import psycopg2

def main():
    db_url = settings.DATABASE_URL
    if not db_url:
        print('ERROR: DATABASE_URL not configured')
        sys.exit(1)
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()
    cur.execute("SELECT column_name, data_type, udt_name FROM information_schema.columns WHERE table_schema='public' AND table_name='zones' ORDER BY ordinal_position")
    rows = cur.fetchall()
    if not rows:
        print('public.zones: (not found)')
    else:
        print('public.zones columns:')
        for col, data_type, udt in rows:
            print(f"- {col}: {data_type} ({udt})")
    cur.close()
    conn.close()

if __name__ == '__main__':
    main()
