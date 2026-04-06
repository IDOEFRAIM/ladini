-- Migration to enable pgvector and store document chunks
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS agri_vector;

CREATE TABLE IF NOT EXISTS agri_vector.document_chunks (
    chunk_id UUID PRIMARY KEY,
    parent_doc_source_id TEXT NOT NULL,
    chunk_index INT NOT NULL,
    text_content TEXT NOT NULL,
    content_hash VARCHAR(32) NOT NULL,
    embedding VECTOR(1536), -- ADA-002 dimension
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    -- Idempotence: we don't want duplicate text chunks
    CONSTRAINT uq_content_hash UNIQUE (content_hash)
);

CREATE INDEX IF NOT EXISTS idx_chunks_source ON agri_vector.document_chunks(parent_doc_source_id);
-- HNSW Index for fast similarity search
CREATE INDEX IF NOT EXISTS idx_chunks_embedding 
ON agri_vector.document_chunks 
USING hnsw (embedding vector_cosine_ops);
