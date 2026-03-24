-- Migration: Market split architecture persistence
-- Adds conversational user context and background match storage.

CREATE SCHEMA IF NOT EXISTS intelligence;

CREATE TABLE IF NOT EXISTS intelligence.user_context (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL,
    last_intent TEXT,
    pending_intent TEXT,
    draft_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT user_context_user_unique UNIQUE (user_id),
    CONSTRAINT user_context_user_fk FOREIGN KEY (user_id)
        REFERENCES public.users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_user_context_pending_intent
    ON intelligence.user_context (pending_intent);
CREATE INDEX IF NOT EXISTS idx_user_context_updated_at
    ON intelligence.user_context (updated_at DESC);

CREATE TABLE IF NOT EXISTS intelligence.matches (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    product_id UUID NOT NULL,
    buyer_id UUID,
    score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'SUGGESTED',
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT matches_product_fk FOREIGN KEY (product_id)
        REFERENCES public.products(id) ON DELETE CASCADE,
    CONSTRAINT matches_buyer_fk FOREIGN KEY (buyer_id)
        REFERENCES public.users(id),
    CONSTRAINT matches_score_range CHECK (score >= 0.0 AND score <= 1.0)
);

CREATE INDEX IF NOT EXISTS idx_matches_status
    ON intelligence.matches (status);
CREATE INDEX IF NOT EXISTS idx_matches_buyer_status
    ON intelligence.matches (buyer_id, status);
CREATE INDEX IF NOT EXISTS idx_matches_product_created
    ON intelligence.matches (product_id, created_at DESC);
