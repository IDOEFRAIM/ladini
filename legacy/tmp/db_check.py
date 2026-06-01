from sqlalchemy import create_engine, text
from agriconnect.core.db import resolve_database_url

url = resolve_database_url(required=False)
engine = create_engine(url)

q1 = text("SELECT to_regclass('ingestion.document_chunks')")
q2 = text("SELECT COUNT(*) FROM ingestion.document_chunks")
q3 = text("SELECT COUNT(*) FROM (SELECT content_hash FROM ingestion.document_chunks GROUP BY content_hash HAVING COUNT(*) > 1) t")
q4 = text(
    """
SELECT c.conname
FROM pg_constraint c
JOIN pg_class t ON t.oid = c.conrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE n.nspname = 'ingestion'
  AND t.relname = 'document_chunks'
  AND c.contype = 'u'
"""
)

with engine.connect() as conn:
    print("table=", conn.execute(q1).scalar())
    print("rows=", conn.execute(q2).scalar())
    print("dup_content_hash_groups=", conn.execute(q3).scalar())
    print("unique_constraints=", [r[0] for r in conn.execute(q4).fetchall()])
