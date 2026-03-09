import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agriconnect.core.settings import settings
import psycopg2

SQL_DIR = Path(__file__).resolve().parents[1] / 'src' / 'agriconnect' / 'database'

FILES_IN_ORDER = [
    'enable_pgvector.sql',
    'init.sql',
    'marketplace_tables_clean.sql',
    'protocol_tables.sql',
    'audit_trail.sql',
    'migration_prisma_align_20260226.sql',
    'migration_prisma_align_tests.sql',
]


def main():
    db_url = settings.DATABASE_URL
    if not db_url:
        print('ERROR: DATABASE_URL not configured')
        sys.exit(1)

    print('Connecting to DB...')
    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    cur = conn.cursor()

    for fname in FILES_IN_ORDER:
        path = SQL_DIR / fname
        if not path.exists():
            print(f'Skipping missing {fname}')
            continue
        print(f'Applying {fname}...')
        sql = path.read_text(encoding='utf-8')
        try:
            cur.execute(sql)
            print(f'  ✅ Applied {fname}')
        except Exception as e:
            print(f'  ⚠️ Error applying {fname}:', e)
            # continue to next file

    cur.close()
    conn.close()
    print('\nAll done.')


if __name__ == '__main__':
    main()
