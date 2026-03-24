import sys
from agriconnect.core.settings import settings
import psycopg2
from psycopg2.extras import RealDictCursor


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

    try:
        cur.execute('SELECT count(*) as c FROM agri_vector.document_chunks')
        print('document_chunks rows:', cur.fetchone()['c'])
    except Exception as e:
        print('document_chunks: ERROR', e)

    try:
        cur.execute('SELECT count(*) as c FROM agri_vector.legacy_rag_import_log')
        print('legacy_rag_import_log rows:', cur.fetchone()['c'])
    except Exception as e:
        print('legacy_rag_import_log: ERROR', e)

    try:
        cur.execute('SELECT source_file, imported_rows, status, imported_at FROM agri_vector.legacy_rag_import_log ORDER BY imported_at DESC LIMIT 10')
        rows = cur.fetchall()
        if rows:
            print('\nLatest import logs:')
            for r in rows:
                print(' -', r['source_file'], '|', r['imported_rows'], 'rows |', r['status'], '|', r['imported_at'])
        else:
            print('\nNo import log entries found.')
    except Exception as e:
        print('legacy_rag_import_log fetch: ERROR', e)

    try:
        cur.execute('SELECT count(distinct doc_ref) as c FROM agri_vector.document_chunks')
        print('\ndistinct doc_ref count:', cur.fetchone()['c'])
    except Exception as e:
        print('distinct doc_ref: ERROR', e)

    cur.close()
    conn.close()


if __name__ == '__main__':
    main()
