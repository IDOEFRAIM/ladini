from sqlalchemy import create_engine, text
from agriconnect.core.db import resolve_database_url

url = resolve_database_url(required=False)
engine = create_engine(url)

q_exists = text("SELECT to_regclass('agri_vector.document_chunks')")
q_cols = text(
    """
SELECT column_name, data_type, udt_name
FROM information_schema.columns
WHERE table_schema='agri_vector' AND table_name='document_chunks'
ORDER BY ordinal_position
"""
)

with engine.connect() as conn:
    print('exists=', conn.execute(q_exists).scalar())
    rows = conn.execute(q_cols).fetchall()
    for r in rows:
        print(r)
