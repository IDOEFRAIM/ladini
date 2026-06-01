-- Massive PDF synchronization library table
-- Run against PostgreSQL (RDS via SSH tunnel)

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE t.typname = 'pdf_sync_status'
          AND n.nspname = 'public'
    ) THEN
        CREATE TYPE public.pdf_sync_status AS ENUM (
            'pending',
            'downloading',
            'uploaded',
            'error'
        );
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS public.pdf_discovery_library (
    url_hash CHAR(32) PRIMARY KEY,
    url TEXT NOT NULL,
    domain TEXT NOT NULL,
    s3_path TEXT,
    status public.pdf_sync_status NOT NULL DEFAULT 'pending',
    error_log TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pdf_discovery_library_status
    ON public.pdf_discovery_library (status);

CREATE INDEX IF NOT EXISTS idx_pdf_discovery_library_domain
    ON public.pdf_discovery_library (domain);

CREATE OR REPLACE FUNCTION public.set_updated_at_pdf_discovery_library()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_pdf_discovery_library_updated_at ON public.pdf_discovery_library;
CREATE TRIGGER trg_pdf_discovery_library_updated_at
BEFORE UPDATE ON public.pdf_discovery_library
FOR EACH ROW
EXECUTE FUNCTION public.set_updated_at_pdf_discovery_library();

COMMIT;
