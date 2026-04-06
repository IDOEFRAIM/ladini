import os, sys, re
from pathlib import Path

def load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line=line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        k,v = line.split('=',1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")

def main():
    root = Path(__file__).resolve().parents[1]
    load_env(root / '.env')
    try:
        import psycopg2
    except Exception as e:
        print('psycopg2 not installed:', e)
        sys.exit(1)
    from psycopg2 import sql
    db = os.environ.get('DATABASE_URL')
    if not db:
        print('DATABASE_URL not set')
        sys.exit(1)
    conn = psycopg2.connect(db)
    cur = conn.cursor()
    print('Tables and id columns:')
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
    tables = [r[0] for r in cur.fetchall()]
    for t in sorted(tables):
        cur.execute(sql.SQL("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='public' AND table_name=%s"), [t])
        cols = cur.fetchall()
        idcols = [c for c in cols if c[0].lower()=='id']
        print(f"- {t}: columns={len(cols)}, id_col_present={bool(idcols)}")
        # print first 5 cols
        print('   ', [c[0] for c in cols[:8]])
    cur.close(); conn.close()

if __name__=='__main__':
    main()
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

    schemas = ['public', 'marketplace', 'intelligence', 'auth']

    print('Listing tables and columns for schemas:', ', '.join(schemas))
    cur.execute(
        """
        SELECT table_schema, table_name, column_name, data_type, udt_name
        FROM information_schema.columns
        WHERE table_schema = ANY(%s)
        ORDER BY table_schema, table_name, ordinal_position
        """,
        (schemas,)
    )

    rows = cur.fetchall()
    last_table = None
    for schema, table, col, data_type, udt in rows:
        if last_table != (schema, table):
            print(f"\n-- {schema}.{table}")
            last_table = (schema, table)
        print(f"{col}: {data_type} ({udt})")

    cur.close()
    conn.close()


if __name__ == '__main__':
    main()
