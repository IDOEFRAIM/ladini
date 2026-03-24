-- Migration: Data-Driven Acquisition with Airflow + Hybrid Storage (PG + PGVector)
-- Date: 2026-03-19

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS ingestion;
CREATE SCHEMA IF NOT EXISTS agri_weather;
CREATE SCHEMA IF NOT EXISTS agri_market;
CREATE SCHEMA IF NOT EXISTS agri_notify;
CREATE SCHEMA IF NOT EXISTS agri_vector;

-- ------------------------------------------------------------------
-- 1) Distributed ingestion state (idempotence by MD5)
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingestion.document_state (
    file_path TEXT PRIMARY KEY,
    md5_hash CHAR(32) NOT NULL,
    status TEXT NOT NULL DEFAULT 'INGESTED',
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_document_state_ingested_at
    ON ingestion.document_state (last_ingested_at DESC);

-- ------------------------------------------------------------------
-- 2) Weather raw timeseries + alerts
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agri_weather.observations (
    id BIGSERIAL PRIMARY KEY,
    zone_name TEXT NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    opencage_place_id TEXT,
    opencage_formatted TEXT,
    observed_at TIMESTAMPTZ NOT NULL,
    forecast_date DATE NOT NULL,
    temperature_c DOUBLE PRECISION,
    precipitation_mm DOUBLE PRECISION,
    humidity_pct DOUBLE PRECISION,
    confidence_score DOUBLE PRECISION NOT NULL DEFAULT 0.85,
    raw_payload_link TEXT,
    bulletin_excerpt TEXT,
    source TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'api_openmeteo',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_weather_humidity_range CHECK (humidity_pct IS NULL OR (humidity_pct >= 0 AND humidity_pct <= 100)),
    CONSTRAINT chk_weather_confidence_range CHECK (confidence_score >= 0 AND confidence_score <= 1),
    UNIQUE (zone_name, observed_at, source)
);

CREATE INDEX IF NOT EXISTS idx_weather_obs_zone_forecast
    ON agri_weather.observations (zone_name, forecast_date DESC);
CREATE INDEX IF NOT EXISTS idx_weather_obs_observed
    ON agri_weather.observations (observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_weather_obs_observed_brin
    ON agri_weather.observations USING BRIN (observed_at);
CREATE INDEX IF NOT EXISTS idx_weather_obs_place_time
    ON agri_weather.observations (opencage_place_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS agri_weather.alerts (
    id BIGSERIAL PRIMARY KEY,
    zone_name TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    details TEXT NOT NULL,
    forecast_date DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_consumed BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_weather_alerts_zone_created
    ON agri_weather.alerts (zone_name, created_at DESC);

-- ------------------------------------------------------------------
-- 3) Market prices + put/call activation signals
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agri_market.prices (
    id BIGSERIAL PRIMARY KEY,
    market_name TEXT NOT NULL,
    zone_name TEXT NOT NULL,
    commodity TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'kg',
    price_value DOUBLE PRECISION NOT NULL,
    currency TEXT NOT NULL DEFAULT 'XOF',
    collected_at TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (market_name, zone_name, commodity, collected_at)
);

CREATE INDEX IF NOT EXISTS idx_market_prices_zone_commodity
    ON agri_market.prices (zone_name, commodity, collected_at DESC);

CREATE TABLE IF NOT EXISTS agri_market.put_call_signals (
    id BIGSERIAL PRIMARY KEY,
    zone_name TEXT NOT NULL,
    commodity TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    trigger_price DOUBLE PRECISION NOT NULL,
    current_price DOUBLE PRECISION NOT NULL,
    details TEXT,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_market_signals_zone_detected
    ON agri_market.put_call_signals (zone_name, detected_at DESC);

-- ------------------------------------------------------------------
-- 4) User matching + notification outbox
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agri_notify.matches (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    zone_name TEXT NOT NULL,
    crop_name TEXT,
    weather_alert_type TEXT,
    market_signal_type TEXT,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dispatched_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_notify_matches_status
    ON agri_notify.matches (status, created_at ASC);

-- ------------------------------------------------------------------
-- 5) Vector chunks in PostgreSQL (hybrid relational + embeddings)
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agri_vector.document_chunks (
    chunk_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_ref TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    category TEXT,
    zone_name TEXT,
    content TEXT NOT NULL,
    content_md5 CHAR(32) NOT NULL,
    embedding VECTOR(384),
    forecast_date DATE,
    valid_until TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_uri TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    chunk_version INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_chunks_docref_md5
    ON agri_vector.document_chunks (doc_ref, content_md5, chunk_version);

CREATE INDEX IF NOT EXISTS idx_document_chunks_validity
    ON agri_vector.document_chunks (is_active, valid_until);

CREATE INDEX IF NOT EXISTS idx_document_chunks_type_zone
    ON agri_vector.document_chunks (doc_type, zone_name);

-- ------------------------------------------------------------------
-- 6) Legacy rag_db JSON migration support table
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agri_vector.legacy_rag_import_log (
    id BIGSERIAL PRIMARY KEY,
    source_file TEXT NOT NULL,
    imported_rows INTEGER NOT NULL DEFAULT 0,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL DEFAULT 'DONE'
);
