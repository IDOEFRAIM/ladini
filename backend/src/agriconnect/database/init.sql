-- ============================================
-- AgriConnect Database Initialization
-- PostgreSQL 16 + pgvector Extension
-- ============================================

-- Enable pgvector extension (must be first)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Set up search path
SET search_path TO public;

-- ============================================
-- 1. ZONES GÉOGRAPHIQUES (Agro-écologiques)
-- ============================================

CREATE TABLE IF NOT EXISTS zones (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL,
    region VARCHAR(100) NOT NULL,
    coordinates JSONB,
    agro_type VARCHAR(50) NOT NULL, -- GRENIER, PASTORAL, COTON, MARAICHAGE
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns WHERE table_name='zones' AND column_name='region'
    ) THEN
        CREATE INDEX IF NOT EXISTS idx_zones_region ON zones(region);
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns WHERE table_name='zones' AND column_name='agro_type'
    ) THEN
        CREATE INDEX IF NOT EXISTS idx_zones_agro_type ON zones(agro_type);
    END IF;
END $$;

-- ============================================
-- 2. UTILISATEURS (Paysans)
-- ============================================

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    phone VARCHAR(20) UNIQUE NOT NULL,
    name VARCHAR(255),
    email VARCHAR(255) UNIQUE,
    password TEXT,
    email_verified TIMESTAMP,
    language VARCHAR(10) DEFAULT 'fr', -- fr, moore, dioula, fulfulde
    role VARCHAR(50) DEFAULT 'PRODUCER',
    
    -- Profil agricole
    zone_id UUID REFERENCES zones(id) ON DELETE SET NULL,
    farm_size_ha DECIMAL(10,2),
    
    -- Préférences notifications
    notify_weather BOOLEAN DEFAULT TRUE,
    notify_pest BOOLEAN DEFAULT TRUE,
    notify_market BOOLEAN DEFAULT TRUE,
    notify_training BOOLEAN DEFAULT TRUE,
    
    -- Metadata
    onboarded BOOLEAN DEFAULT FALSE,
    last_active TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='phone') THEN
        CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone);
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='zone_id') THEN
        CREATE INDEX IF NOT EXISTS idx_users_zone ON users(zone_id);
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='last_active') THEN
        CREATE INDEX IF NOT EXISTS idx_users_active ON users(last_active);
    END IF;
END $$;

-- Authentication tables (NextAuth-style)
CREATE TABLE IF NOT EXISTS accounts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type VARCHAR(50) NOT NULL,
    provider VARCHAR(100) NOT NULL,
    provider_account_id VARCHAR(255) NOT NULL,
    refresh_token TEXT,
    access_token TEXT,
    expires_at INT,
    token_type VARCHAR(50),
    scope TEXT,
    id_token TEXT,
    session_state TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_provider ON accounts(provider, provider_account_id);

CREATE TABLE IF NOT EXISTS sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_token VARCHAR(255) UNIQUE NOT NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- ============================================
-- 3. CULTURES DES UTILISATEURS
-- ============================================

CREATE TABLE IF NOT EXISTS user_crops (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    crop_name VARCHAR(100) NOT NULL,
    variety VARCHAR(100),
    planting_date DATE,
    expected_harvest DATE,
    area_ha DECIMAL(10,2),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    
    UNIQUE(user_id, crop_name)
);

CREATE INDEX idx_user_crops_user ON user_crops(user_id);
CREATE INDEX idx_user_crops_crop ON user_crops(crop_name);

-- ============================================
-- 4. SYSTÈME D'ALERTES (Proactif)
-- ============================================

CREATE TABLE IF NOT EXISTS alerts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    
    -- Classification
    type VARCHAR(50) NOT NULL, -- WEATHER, PEST, DISEASE, PRICE_DROP, MARKET_OPP, TRAINING
    severity VARCHAR(20) NOT NULL, -- INFO, WARNING, CRITICAL
    
    -- Contenu
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    voice_url TEXT,
    
    -- Ciblage
    zone_id UUID REFERENCES zones(id) ON DELETE CASCADE,
    target_crops TEXT[], -- Array of crop names
    
    -- Validité
    start_date TIMESTAMP DEFAULT NOW(),
    end_date TIMESTAMP,
    is_active BOOLEAN DEFAULT TRUE,
    
    -- Tracking
    sent_count INTEGER DEFAULT 0,
    read_count INTEGER DEFAULT 0,
    
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_alerts_type ON alerts(type);
CREATE INDEX idx_alerts_zone ON alerts(zone_id);
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='alerts' AND column_name='is_active') THEN
        CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts(is_active, start_date, end_date);
    END IF;
END $$;
CREATE INDEX idx_alerts_severity ON alerts(severity);

-- ============================================
-- 5. CONVERSATIONS (Historique)
-- ============================================

CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    
    -- Message
    query TEXT NOT NULL,
    response TEXT,
    agent_type VARCHAR(50), -- sentinel, doctor, market, formation
    
    -- Context
    zone_id UUID REFERENCES zones(id),
    crop VARCHAR(100),
    
    -- Metadata
    mode VARCHAR(20) DEFAULT 'text', -- text, voice, sms
    async_mode BOOLEAN DEFAULT FALSE,
    execution_path JSONB,
    
    -- Performance
    response_time_ms INTEGER,
    
    created_at TIMESTAMP DEFAULT NOW()
);

DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='conversations' AND column_name='user_id'
    ) THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_conversations_user ON ' || quote_ident(current_schema()) || '.conversations(' || quote_ident('user_id') || ')';
    ELSIF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='conversations' AND column_name='userId'
    ) THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_conversations_user ON ' || quote_ident(current_schema()) || '.conversations(' || quote_ident('userId') || ')';
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='conversations' AND column_name='created_at'
    ) THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_conversations_created ON ' || quote_ident(current_schema()) || '.conversations(' || quote_ident('created_at') || ' DESC)';
    ELSIF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='conversations' AND column_name='createdAt'
    ) THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_conversations_created ON ' || quote_ident(current_schema()) || '.conversations(' || quote_ident('createdAt') || ' DESC)';
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='conversations' AND column_name='agent_type'
    ) THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_conversations_agent ON ' || quote_ident(current_schema()) || '.conversations(' || quote_ident('agent_type') || ')';
    ELSIF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='conversations' AND column_name='agentType'
    ) THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_conversations_agent ON ' || quote_ident(current_schema()) || '.conversations(' || quote_ident('agentType') || ')';
    END IF;
END $$;

-- ============================================
-- 6. DONNÉES MÉTÉO (Cache)
-- ============================================

CREATE TABLE IF NOT EXISTS weather_data (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    zone_id UUID REFERENCES zones(id) ON DELETE CASCADE,
    
    -- Forecast data
    date DATE NOT NULL,
    temperature_max DECIMAL(5,2),
    temperature_min DECIMAL(5,2),
    precipitation_mm DECIMAL(6,2),
    humidity_pct DECIMAL(5,2),
    wind_speed_kmh DECIMAL(5,2),
    
    -- Source
    source VARCHAR(100), -- openmeteo, earth_engine, meteo_burkina
    raw_data JSONB,
    
    created_at TIMESTAMP DEFAULT NOW(),
    
    UNIQUE(zone_id, date, source)
);

DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='weather_data' AND column_name='zone_id'
    ) AND EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='weather_data' AND column_name='date'
    ) THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_weather_zone_date ON ' || quote_ident(current_schema()) || '.weather_data(' || quote_ident('zone_id') || ', ' || quote_ident('date') || ' DESC)';
    ELSIF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='weather_data' AND column_name='zoneId'
    ) AND EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='weather_data' AND column_name='date'
    ) THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_weather_zone_date ON ' || quote_ident(current_schema()) || '.weather_data(' || quote_ident('zoneId') || ', ' || quote_ident('date') || ' DESC)';
    END IF;
END $$;

-- ============================================
-- 7. PRIX DE MARCHÉ (Cache)
-- ============================================

CREATE TABLE IF NOT EXISTS market_prices (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- Keep as TEXT for compatibility with legacy schemas where zones.id is VARCHAR/TEXT
    zone_id TEXT,
    
    -- Product
    product_name VARCHAR(100) NOT NULL,
    unit VARCHAR(20) DEFAULT 'kg',
    
    -- Prices
    price_min DECIMAL(10,2),
    price_avg DECIMAL(10,2),
    price_max DECIMAL(10,2),
    currency VARCHAR(10) DEFAULT 'FCFA',
    
    -- Market info
    market_name VARCHAR(255),
    observation_date DATE NOT NULL,
    
    -- Source
    source VARCHAR(100), -- sim, sonagess, fews
    raw_data JSONB,
    
    created_at TIMESTAMP DEFAULT NOW(),
    
    UNIQUE(zone_id, product_name, observation_date, source)
);

CREATE INDEX idx_market_zone_product ON market_prices(zone_id, product_name, observation_date DESC);

-- ============================================
-- 8. OFFRES DE SURPLUS (Proactif — MarketCoach)
-- ============================================

CREATE TABLE IF NOT EXISTS surplus_offers (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(255) DEFAULT 'anonymous',
    product_name VARCHAR(255) NOT NULL,
    quantity_kg DECIMAL(10,2) NOT NULL,
    price_kg DECIMAL(10,2),
    zone_id UUID REFERENCES zones(id),
    location VARCHAR(255),
    status VARCHAR(20) DEFAULT 'OPEN',  -- OPEN, MATCHED, SOLD, EXPIRED
    channel VARCHAR(20) DEFAULT 'api',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='surplus_offers' AND column_name='status') THEN
        CREATE INDEX IF NOT EXISTS idx_surplus_status ON surplus_offers(status);
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='surplus_offers' AND column_name='product_name') THEN
        CREATE INDEX IF NOT EXISTS idx_surplus_product ON surplus_offers(product_name);
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='surplus_offers' AND column_name='zone_id') THEN
        CREATE INDEX IF NOT EXISTS idx_surplus_zone ON surplus_offers(zone_id);
    END IF;
END $$;

-- ============================================
-- 9. DIAGNOSTICS SOL (Proactif — AgriSoilAgent)
-- ============================================

CREATE TABLE IF NOT EXISTS soil_diagnoses (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(255) DEFAULT 'anonymous',
    zone_id UUID REFERENCES zones(id),
    village VARCHAR(255),
    soil_type VARCHAR(100),
    fertility VARCHAR(100),
    ph_alert VARCHAR(100),
    water_strategy VARCHAR(255),
    adapted_crops JSONB,
    raw_diagnosis JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_soil_diag_village ON soil_diagnoses(village);
CREATE INDEX idx_soil_diag_zone ON soil_diagnoses(zone_id);

-- ============================================
-- 10. DIAGNOSTICS PLANTE (Proactif — PlantHealthDoctor)
-- ============================================

CREATE TABLE IF NOT EXISTS plant_diagnoses (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(255) DEFAULT 'anonymous',
    crop_name VARCHAR(100),
    disease_name VARCHAR(255),
    severity VARCHAR(50),
    treatment_bio TEXT,
    treatment_chimique TEXT,
    estimated_cost DECIMAL(10,2),
    raw_diagnosis JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_plant_diag_crop ON plant_diagnoses(crop_name);
CREATE INDEX idx_plant_diag_disease ON plant_diagnoses(disease_name);

-- ============================================
-- 11. RAPPELS (Proactif — Reminders)
-- ============================================

CREATE TABLE IF NOT EXISTS reminders (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    scheduled_at TIMESTAMP WITH TIME ZONE NOT NULL,
    sent BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_reminders_user ON reminders(user_id);
CREATE INDEX idx_reminders_scheduled ON reminders(scheduled_at, sent);

-- ============================================
-- DOCUMENTS RAG (Vector Store Metadata)
-- ============================================

CREATE TABLE IF NOT EXISTS rag_documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    
    -- Document info
    title TEXT NOT NULL,
    content TEXT,
    doc_type VARCHAR(50), -- bulletin, guide, research, market_report
    
    -- Metadata
    source_url TEXT,
    author VARCHAR(255),
    publication_date DATE,
    language VARCHAR(10) DEFAULT 'fr',
    
    -- Vector embedding (stored in rag_db/ via LlamaIndex)
    embedding_id VARCHAR(255), -- Reference to vector store
    
    -- Stats
    access_count INTEGER DEFAULT 0,
    last_accessed TIMESTAMP,
    
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_rag_docs_type ON rag_documents(doc_type);
CREATE INDEX idx_rag_docs_date ON rag_documents(publication_date DESC);
CREATE INDEX idx_rag_docs_embedding ON rag_documents(embedding_id);

-- ============================================
-- SCRAPER JOBS (Tracking)
-- ============================================

CREATE TABLE IF NOT EXISTS scraper_jobs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_name VARCHAR(100) NOT NULL,
    status VARCHAR(20) NOT NULL, -- RUNNING, SUCCESS, FAILED
    
    -- Results
    documents_collected INTEGER DEFAULT 0,
    errors JSONB,
    
    -- Timing
    started_at TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP,
    duration_seconds INTEGER,
    
    -- Report
    report JSONB
);

CREATE INDEX idx_scraper_jobs_name ON scraper_jobs(job_name, started_at DESC);
CREATE INDEX idx_scraper_jobs_status ON scraper_jobs(status);

-- ============================================
-- SEED DATA (Zones Agro-écologiques Burkina Faso)
-- ============================================

DO $$
DECLARE
    has_region BOOLEAN;
    has_agro_type BOOLEAN;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='zones' AND column_name='region'
    ) INTO has_region;

    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='zones' AND column_name='agro_type'
    ) INTO has_agro_type;

    IF has_region AND has_agro_type THEN
        INSERT INTO zones (name, region, agro_type) VALUES
        ('Ouagadougou - Centre', 'Centre', 'MARAICHAGE'),
        ('Bobo-Dioulasso - Hauts-Bassins', 'Hauts-Bassins', 'COTON'),
        ('Dedougou - Boucle du Mouhoun', 'Boucle du Mouhoun', 'GRENIER'),
        ('Fada N''Gourma - Est', 'Est', 'PASTORAL'),
        ('Koudougou - Centre-Ouest', 'Centre-Ouest', 'GRENIER'),
        ('Ouahigouya - Nord', 'Nord', 'PASTORAL'),
        ('Dori - Sahel', 'Sahel', 'PASTORAL'),
        ('Banfora - Cascades', 'Cascades', 'MARAICHAGE'),
        ('Kaya - Centre-Nord', 'Centre-Nord', 'GRENIER'),
        ('Gaoua - Sud-Ouest', 'Sud-Ouest', 'COTON'),
        ('Tenkodogo - Centre-Est', 'Centre-Est', 'GRENIER'),
        ('Manga - Centre-Sud', 'Centre-Sud', 'MARAICHAGE'),
        ('Djibo - Sahel', 'Sahel', 'PASTORAL')
        ON CONFLICT DO NOTHING;
    ELSIF has_agro_type THEN
        INSERT INTO zones (name, agro_type) VALUES
        ('Ouagadougou - Centre', 'MARAICHAGE'),
        ('Bobo-Dioulasso - Hauts-Bassins', 'COTON'),
        ('Dedougou - Boucle du Mouhoun', 'GRENIER'),
        ('Fada N''Gourma - Est', 'PASTORAL'),
        ('Koudougou - Centre-Ouest', 'GRENIER'),
        ('Ouahigouya - Nord', 'PASTORAL'),
        ('Dori - Sahel', 'PASTORAL'),
        ('Banfora - Cascades', 'MARAICHAGE'),
        ('Kaya - Centre-Nord', 'GRENIER'),
        ('Gaoua - Sud-Ouest', 'COTON'),
        ('Tenkodogo - Centre-Est', 'GRENIER'),
        ('Manga - Centre-Sud', 'MARAICHAGE'),
        ('Djibo - Sahel', 'PASTORAL')
        ON CONFLICT DO NOTHING;
    ELSE
        -- Legacy schema without expected columns/defaults: skip seed instead of failing migration.
        RAISE NOTICE 'Skipping zones seed: expected columns (region/agro_type) are missing.';
    END IF;
END $$;

-- ============================================
-- FUNCTIONS & TRIGGERS
-- ============================================

-- Auto-update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_zones_updated_at BEFORE UPDATE ON zones FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_users_updated_at BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_user_crops_updated_at BEFORE UPDATE ON user_crops FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_alerts_updated_at BEFORE UPDATE ON alerts FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_rag_documents_updated_at BEFORE UPDATE ON rag_documents FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================
-- GRANTS (User permissions)
-- ============================================

GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO agriconnect;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO agriconnect;
GRANT USAGE ON SCHEMA public TO agriconnect;

-- ============================================
-- SUCCESS MESSAGE
-- ============================================

DO $$
BEGIN
    RAISE NOTICE '✅ AgriConnect Database initialized successfully!';
    RAISE NOTICE '📊 Tables: zones, users, user_crops, alerts, conversations, weather_data, market_prices, rag_documents, scraper_jobs';
    RAISE NOTICE '🔌 Extensions: vector (pgvector), uuid-ossp';
    RAISE NOTICE '🌍 Seeded 13 zones agro-écologiques du Burkina Faso';
END $$;
