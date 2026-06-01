BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE t.typname = 'pdf_sync_status'
          AND n.nspname = 'public'
    )
    AND NOT EXISTS (
        SELECT 1
        FROM pg_enum e
        JOIN pg_type t ON t.oid = e.enumtypid
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE t.typname = 'pdf_sync_status'
          AND n.nspname = 'public'
          AND e.enumlabel = 'indexed'
    ) THEN
        ALTER TYPE public.pdf_sync_status ADD VALUE 'indexed';
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS public.document_chunks (
    id BIGSERIAL PRIMARY KEY,
    document_id CHAR(32) NOT NULL,
    source_s3_path TEXT NOT NULL,
    domain TEXT NOT NULL,
    category TEXT NOT NULL,
    page_number INTEGER,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding VECTOR(1536) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_document_chunks_document
        FOREIGN KEY (document_id)
        REFERENCES public.pdf_discovery_library(url_hash)
        ON DELETE CASCADE,
    CONSTRAINT uq_document_chunk UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_document_chunks_document_id
    ON public.document_chunks (document_id);

CREATE INDEX IF NOT EXISTS idx_document_chunks_category
    ON public.document_chunks (category);

CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding_hnsw
    ON public.document_chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE OR REPLACE FUNCTION public.set_updated_at_document_chunks()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_document_chunks_updated_at ON public.document_chunks;
CREATE TRIGGER trg_document_chunks_updated_at
BEFORE UPDATE ON public.document_chunks
FOR EACH ROW
EXECUTE FUNCTION public.set_updated_at_document_chunks();

COMMIT;