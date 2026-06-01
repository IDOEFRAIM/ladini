from sqlalchemy import create_engine, text
from agriconnect.core.db import resolve_database_url

url = resolve_database_url(required=False)
engine = create_engine(url)

ddl = [
    "ALTER TABLE ingestion.document_chunks ADD COLUMN IF NOT EXISTS content_hash TEXT",
    "ALTER TABLE ingestion.document_chunks ADD COLUMN IF NOT EXISTS embedding vector(768)",
    "ALTER TABLE ingestion.document_chunks ADD COLUMN IF NOT EXISTS trace_id VARCHAR(64)",
    "ALTER TABLE ingestion.document_chunks ADD COLUMN IF NOT EXISTS is_indexed BOOLEAN",
    "ALTER TABLE ingestion.document_chunks ALTER COLUMN is_indexed SET DEFAULT false",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_ingestion_document_chunks_content_hash ON ingestion.document_chunks (content_hash)",
]

with engine.begin() as conn:
    for stmt in ddl:
        conn.execute(text(stmt))

print("patched ingestion.document_chunks")
