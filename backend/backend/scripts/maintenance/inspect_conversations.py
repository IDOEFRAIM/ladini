import os, psycopg2

def load_env():
    env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
    if os.path.exists(env_path):
        with open(env_path, encoding='utf-8') as f:
            for line in f.read().splitlines():
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k,v = line.split('=',1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_env()

DB = os.environ.get('DATABASE_URL')
if not DB:
    print('DATABASE_URL not set'); exit(1)
conn = psycopg2.connect(DB)
cur = conn.cursor()
cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name='conversations' ORDER BY ordinal_position;")
print('Columns:')
for r in cur.fetchall():
    print(' -', r[0], r[1])

cur.execute("SELECT conname, contype, pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON c.conrelid = t.oid WHERE t.relname='conversations';")
print('\nConstraints:')
for r in cur.fetchall():
    print(' -', r[0], r[1], r[2])

cur.close(); conn.close()
