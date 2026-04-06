import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agriconnect.core.settings import settings
import psycopg2

TABLES = [
    'crop_tech_sheets',
    'diagnostics',
    'action_plans',
    'user_memories',
    'formation_feedback'
]


def main():
    db_url = settings.DATABASE_URL
    if not db_url:
        print('ERROR: DATABASE_URL not configured')
        return 1
    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    cur = conn.cursor()

    print('Checking tables:')
    for t in TABLES:
        cur.execute("SELECT to_regclass(%s)", (f'public.{t}',))
        r = cur.fetchone()[0]
        print(f' - {t}:', 'FOUND' if r else 'MISSING')

    # Run transactional smoke test for diagnostics -> action_plans
    try:
        cur.execute('BEGIN')
        cur.execute("INSERT INTO diagnostics (user_id, crop_name, zone_id, query, diagnosis) VALUES (gen_random_uuid(), 'maize', NULL, 'leaf spots', 'possible fungal') RETURNING id")
        diag_id = cur.fetchone()[0]
        cur.execute("INSERT INTO action_plans (diagnostic_id, actions_immediate, actions_7_days, risks, created_by) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (diag_id, '{"1":"remove infected leaves"}' , '{"1":"apply biocontrol"}', '{"risk":"mild"}', 'test'))
        ap_id = cur.fetchone()[0]
        cur.execute("SELECT id, diagnostic_id FROM action_plans WHERE id=%s", (ap_id,))
        row = cur.fetchone()
        print('\nSmoke test: inserted action_plan id=', row[0], 'linked diag=', row[1])
        cur.execute('ROLLBACK')
    except Exception as e:
        print('Smoke test failed:', e)
        cur.execute('ROLLBACK')
        cur.close()
        conn.close()
        return 2

    cur.close()
    conn.close()
    print('\nVerification complete: OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
