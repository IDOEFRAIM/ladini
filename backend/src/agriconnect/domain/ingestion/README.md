# AgriConnect Ingestion Domain

This folder contains the ingestion pipeline pieces that take `RawDocument` objects (produced by scrapers)
and transform them into persisted artifacts (S3 raw payloads, DB records, and vector store entries for RAG).

Key concepts
- `processors/` — modular content processors (news, pdf, weather, fews). Each processor is responsible for
  splitting, cleaning and producing `document_chunks` for the vector store.
- `storage/` — adapters for S3 and the vector DB: `S3Manager`, `VectorDBManager`.
- `worker.py` — orchestration worker that accepts scraped `RawDocument` objects and runs configured processors
  and storage steps.

Important files
- [backend/src/agriconnect/domain/ingestion/processors](backend/src/agriconnect/domain/ingestion/processors)
- [backend/src/agriconnect/domain/ingestion/storage/s3_manager.py](backend/src/agriconnect/domain/ingestion/storage/s3_manager.py)
- [backend/src/agriconnect/domain/ingestion/storage/vector_db.py](backend/src/agriconnect/domain/ingestion/storage/vector_db.py)

Quick verification

These helper scripts validate the pipeline and vector store integration. Run from the repository root with your virtualenv activated:

```powershell
# verify S3 + vector store + embedding model connectivity
python backend/scripts/verify_pipeline.py

# run ingestion integrity tests (sample data)
python backend/scripts/test_ingestion_integrity.py
```

Production notes
- The ingestion pipeline expects the database and vector store schemas to include the ingestion-specific fields
  (e.g. `content_hash`, embedding columns / indexes). If you plan a production E2E run that writes to Postgres/pgvector
  or RedisSearch, ensure the DB migrations in `backend/alembic/versions` are applied.
- Embedding dimensionality for RAG is controlled by `RAG_EMBEDDING_DIM` in settings; ensure model + index match.

Best practices
- Keep processors idempotent and avoid side effects; persist only through `S3Manager` / `VectorDBManager`.
- When debugging, run processors locally with a single `RawDocument` to inspect chunking and metadata.

If you want assistance creating a minimal DB migration for ingestion (content_hash / embedding column), ask and I can scaffold it.
