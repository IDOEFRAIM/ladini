import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import json
from agriconnect.core.settings import settings
import psycopg2

try:
    db_url = settings.DATABASE_URL
    print(json.dumps({"database_url": db_url}))
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()
    results = {}
    try:
        cur.execute('SELECT COUNT(*) FROM agri_vector.document_chunks')
        results['document_chunks_count'] = cur.fetchone()[0]
    except Exception as e:
        results['document_chunks_count_error'] = str(e)
    try:
        cur.execute('SELECT source_file, imported_rows, status, created_at FROM agri_vector.legacy_rag_import_log ORDER BY created_at DESC LIMIT 5')
        rows = cur.fetchall()
        results['legacy_rag_import_log'] = rows
    except Exception as e:
        results['legacy_rag_import_log_error'] = str(e)
    cur.close()
    conn.close()
    print(json.dumps(results, default=str, ensure_ascii=False, indent=2))
except Exception as e:
    print(json.dumps({"error": str(e)}))
    raise
