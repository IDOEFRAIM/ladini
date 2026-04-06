-- ============================================
-- Enable pgvector Extension
-- PostgreSQL 16+
-- ============================================

-- Create extension (requires superuser or rds_superuser)
CREATE EXTENSION IF NOT EXISTS vector;

-- Verify installation
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_extension WHERE extname = 'vector'
    ) THEN
        RAISE NOTICE '✅ pgvector extension enabled successfully';
        RAISE NOTICE '📦 Version: %', (SELECT extversion FROM pg_extension WHERE extname = 'vector');
    ELSE
        RAISE EXCEPTION '❌ Failed to enable pgvector extension';
    END IF;
END $$;

-- Create vector index for RAG (example - adjust dimensions based on your embeddings)
-- Uncomment and modify when you have vector columns:

-- CREATE TABLE IF NOT EXISTS embeddings_example (
--     id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
--     content TEXT,
--     embedding vector(1536)  -- Adjust dimension for your model (OpenAI ada-002: 1536, MiniLM: 384)
-- );

-- CREATE INDEX ON embeddings_example USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Grant permissions
GRANT USAGE ON SCHEMA public TO agriconnect;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO agriconnect;
