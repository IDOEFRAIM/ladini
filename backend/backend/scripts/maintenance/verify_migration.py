import sys
from agriconnect.core.settings import settings
import psycopg2
from psycopg2.extras import RealDictCursor

KEY_TABLES = [
    ('ingestion', 'document_state'),
    ('agri_vector', 'document_chunks'),
    ('agri_vector', 'legacy_rag_import_log'),
    ('agri_weather', 'observations'),
    ('agri_weather', 'alerts'),
    ('agri_market', 'prices'),
    ('agri_market', 'put_call_signals'),
    ('agri_notify', 'matches'),
]

EXTENSIONS = ['vector', 'pgcrypto']


def main():
    db_url = settings.DATABASE_URL
    if not db_url:
        print('ERROR: DATABASE_URL not configured')
        sys.exit(1)

    try:
        conn = psycopg2.connect(db_url)
    except Exception as e:
        print('ERROR: cannot connect to DB:', e)
        sys.exit(1)

    cur = conn.cursor(cursor_factory=RealDictCursor)

    print('Checking extensions...')
    cur.execute("SELECT extname FROM pg_extension")
    installed = {r['extname'] for r in cur.fetchall()}
    for ext in EXTENSIONS:
        print(f" - {ext}:", 'OK' if ext in installed else 'MISSING')

    print('\nChecking key tables:')
    for schema, table in KEY_TABLES:
        cur.execute(
            "SELECT to_regclass(%s) as exists_regclass",
            (f'{schema}.{table}',)
        )
        exists = cur.fetchone()['exists_regclass'] is not None
        print(f" - {schema}.{table}:", 'FOUND' if exists else 'MISSING')

    print('\nCounts for present tables (fast check):')
    for schema, table in KEY_TABLES:
        try:
            cur.execute(f"SELECT count(*) as c FROM {schema}.{table} LIMIT 1")
            c = cur.fetchone()['c']
            print(f" - {schema}.{table}: {c} rows")
        except Exception as e:
            print(f" - {schema}.{table}: cannot count ({e})")

    cur.close()
    conn.close()


if __name__ == '__main__':
    main()
