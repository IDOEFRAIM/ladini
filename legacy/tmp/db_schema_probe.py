from sqlalchemy import create_engine, text
from agriconnect.core.db import resolve_database_url

url = resolve_database_url(required=False)
engine = create_engine(url)

q_tables = text(
    """
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'ingestion'
ORDER BY table_name
"""
)

q_cols = text(
    """
SELECT table_name, column_name, data_type, udt_name
FROM information_schema.columns
WHERE table_schema = 'ingestion'
  AND table_name IN ('document_chunks', 'document_chunks_v2')
ORDER BY table_name, ordinal_position
"""
)

with engine.connect() as conn:
    tables = [r[0] for r in conn.execute(q_tables).fetchall()]
    print("tables:", tables)
    print("columns:")
    for row in conn.execute(q_cols).fetchall():
        print(row)
