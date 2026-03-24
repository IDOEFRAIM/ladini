import os
import psycopg2
from urllib.parse import urlparse

def main():
    # load backend/.env if present
    env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
    if os.path.exists(env_path):
        with open(env_path, encoding='utf-8') as f:
            for line in f.read().splitlines():
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k,v = line.split('=',1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    db = os.environ.get('DATABASE_URL')
    if not db:
        print('DATABASE_URL not set')
        return
    conn = psycopg2.connect(db)
    cur = conn.cursor()
    cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name='episodic_memories' ORDER BY ordinal_position;")
    rows = cur.fetchall()
    print('Columns for episodic_memories:')
    for r in rows:
        print(' -', r[0], r[1])
    cur.close()
    conn.close()

if __name__=='__main__':
    main()
