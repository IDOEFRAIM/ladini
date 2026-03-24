import os
import sys
import re
import runpy
import secrets
from pathlib import Path


def load_env(path: Path) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(.*?)=(.*)$", line)
        if not m:
            continue
        key, val = m.group(1).strip(), m.group(2).strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        os.environ[key] = val


def main():
    root = Path(__file__).resolve().parents[1]
    env_path = root / ".env"
    load_env(env_path)
    # ensure package imports resolve
    src_path = str(root / "src")
    if src_path not in sys.path:
        sys.path.insert(0, src_path)

    try:
        from agriconnect.core.settings import settings
    except Exception as e:
        print("Failed importing settings:", e, file=sys.stderr)
        sys.exit(1)

    db_url = settings.DATABASE_URL
    if not db_url:
        print('DATABASE_URL not configured', file=sys.stderr)
        sys.exit(1)

    # create role if not exists
    try:
        import psycopg2
    except Exception as e:
        print('psycopg2 not available:', e, file=sys.stderr)
        sys.exit(1)

    # connect and create role
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        cur = conn.cursor()
        role_name = 'agriconnect'
        # generate a password but do not expose it unnecessarily
        pwd = secrets.token_urlsafe(16)
        sql = f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role_name}') THEN CREATE ROLE {role_name} WITH LOGIN PASSWORD '{pwd}'; END IF; END $$;"
        print('Creating role agriconnect if missing...')
        cur.execute(sql)
        cur.close()
        conn.close()
        print('Role ensured (created if missing).')
    except Exception as e:
        print('Failed to create role:', e, file=sys.stderr)
        # continue to attempt migrations anyway

    # run migrations
    script_path = root / 'scripts' / 'apply_sql_migrations.py'
    if not script_path.exists():
        print('apply_sql_migrations.py not found', file=sys.stderr)
        sys.exit(1)
    print('Running SQL migrations...')
    runpy.run_path(str(script_path), run_name='__main__')


if __name__ == '__main__':
    main()
