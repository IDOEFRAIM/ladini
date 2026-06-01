-- Initialize document_chunks table for RAG ingestion (Docling pipeline)
-- Requires: pgvector extension

CREATE EXTENSION IF NOT EXISTS vector;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_type WHERE typname = 'pdf_sync_status'
    ) THEN
        CREATE TYPE public.pdf_sync_status AS ENUM ('pending','downloading','uploaded','error','indexed');
    ELSE
        BEGIN
            ALTER TYPE public.pdf_sync_status ADD VALUE IF NOT EXISTS 'indexed';
        EXCEPTION WHEN duplicate_object THEN NULL;
        END;
    END IF;
END$$;

CREATE TABLE IF NOT EXISTS public.document_chunks (
    id BIGSERIAL PRIMARY KEY,
    document_id TEXT NOT NULL,
    source_s3_path TEXT NOT NULL,
    domain TEXT,
    category TEXT,
    page_number INT,
    chunk_index INT NOT NULL,
    content TEXT NOT NULL,
    metadata JSONB,
    embedding vector(1536),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (document_id, chunk_index)
);

-- HNSW index for fast similarity search
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relname = 'document_chunks_embedding_hnsw'
    ) THEN
        -- Specify operator class for hnsw index (choose cosine ops for similarity)
        CREATE INDEX document_chunks_embedding_hnsw ON public.document_chunks USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
    END IF;
END$$;

-- Trigger to update updated_at
CREATE OR REPLACE FUNCTION public.update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_update_updated_at ON public.document_chunks;
CREATE TRIGGER trg_update_updated_at
BEFORE UPDATE ON public.document_chunks
FOR EACH ROW
EXECUTE FUNCTION public.update_updated_at_column();
