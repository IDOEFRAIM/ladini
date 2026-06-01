-- Text-only / table-focused chunk store for RAG ingestion.
-- Expected workflow: Docling markdown -> chunk -> embedding -> document_chunks.

CREATE EXTENSION IF NOT EXISTS vector;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'pdf_sync_status') THEN
        BEGIN
            ALTER TYPE public.pdf_sync_status ADD VALUE IF NOT EXISTS 'indexed';
        EXCEPTION WHEN duplicate_object THEN NULL;
        END;
    END IF;
END$$;

CREATE TABLE IF NOT EXISTS public.document_chunks (
    id BIGSERIAL PRIMARY KEY,
    parent_id TEXT NOT NULL,
    chunk_index INT NOT NULL,
    content TEXT NOT NULL,
    embedding vector(768),
    metadata JSONB,
    page_number INT,
    source_domain TEXT,
    category TEXT,
    source_s3_path TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (parent_id, chunk_index)
);

-- Compatibility migration for pre-existing document_chunks schema.
ALTER TABLE public.document_chunks ADD COLUMN IF NOT EXISTS parent_id TEXT;
ALTER TABLE public.document_chunks ADD COLUMN IF NOT EXISTS document_id TEXT;
ALTER TABLE public.document_chunks ADD COLUMN IF NOT EXISTS source_domain TEXT;
ALTER TABLE public.document_chunks ADD COLUMN IF NOT EXISTS domain TEXT;
ALTER TABLE public.document_chunks ADD COLUMN IF NOT EXISTS source_s3_path TEXT;
ALTER TABLE public.document_chunks ADD COLUMN IF NOT EXISTS page_number INT;
ALTER TABLE public.document_chunks ADD COLUMN IF NOT EXISTS category TEXT;
ALTER TABLE public.document_chunks ADD COLUMN IF NOT EXISTS metadata JSONB;

-- Backfill compatibility values when old columns exist.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'document_chunks' AND column_name = 'document_id'
    ) THEN
        EXECUTE 'UPDATE public.document_chunks SET parent_id = document_id::text WHERE parent_id IS NULL';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'document_chunks' AND column_name = 'domain'
    ) THEN
        EXECUTE 'UPDATE public.document_chunks SET source_domain = domain WHERE source_domain IS NULL';
    END IF;
END$$;

DROP INDEX IF EXISTS public.document_chunks_parent_chunk_uniq;
CREATE UNIQUE INDEX IF NOT EXISTS document_chunks_parent_chunk_uniq
    ON public.document_chunks(parent_id, chunk_index);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = 'document_chunks_embedding_hnsw'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX document_chunks_embedding_hnsw
            ON public.document_chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
    END IF;
END$$;

CREATE INDEX IF NOT EXISTS document_chunks_parent_id_idx ON public.document_chunks(parent_id);

CREATE OR REPLACE FUNCTION public.set_updated_at_document_chunks()
RETURNS trigger AS $$
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
