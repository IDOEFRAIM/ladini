-- ============================================================
-- marketplace.sql — Tables Agribusiness AgriConnect
-- Alignées avec le schema Prisma frontend.
-- Monnaie : FCFA.  Identifiant : UUID v4.
-- ============================================================

-- 1. Régions climatiques
CREATE TABLE IF NOT EXISTS climatic_regions (
    id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 2. Extension table zones (ajout de la FK climatic_region)
-- La table zones existe déjà ; on ajoute la colonne si absente.
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'zones' AND column_name = 'climatic_region_id'
    ) THEN
        ALTER TABLE zones ADD COLUMN climatic_region_id TEXT REFERENCES climatic_regions(id);
    END IF;
END $$;

-- 3. Producteurs
CREATE TABLE IF NOT EXISTS producers (
    id         TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    user_id    TEXT NOT NULL REFERENCES users(id),
    zone_id    TEXT REFERENCES zones(id),
    status     TEXT DEFAULT 'PENDING' CHECK (status IN ('PENDING','ACTIVE','SUSPENDED')),
    bio        TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_producers_user ON producers(user_id);

-- 4. Fermes
CREATE TABLE IF NOT EXISTS farms (
    id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    name        TEXT NOT NULL DEFAULT 'Ma ferme',
    producer_id TEXT NOT NULL REFERENCES producers(id),
    zone_id     TEXT REFERENCES zones(id),
    latitude    DOUBLE PRECISION,
    longitude   DOUBLE PRECISION,
    size_ha     DOUBLE PRECISION,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_farms_producer ON farms(producer_id);

-- 5. Stocks
CREATE TABLE IF NOT EXISTS stocks (
    id         TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    farm_id    TEXT NOT NULL REFERENCES farms(id),
    item_name  TEXT NOT NULL,
    quantity   DOUBLE PRECISION DEFAULT 0,
    unit       TEXT DEFAULT 'kg',
    type       TEXT DEFAULT 'HARVEST' CHECK (type IN ('HARVEST','INPUT','EQUIPMENT')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_stocks_farm ON stocks(farm_id);

-- 6. Mouvements de stock
CREATE TABLE IF NOT EXISTS stock_movements (
    id         TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    stock_id   TEXT NOT NULL REFERENCES stocks(id),
    type       TEXT NOT NULL CHECK (type IN ('IN','OUT','WASTE')),
    quantity   DOUBLE PRECISION NOT NULL,
    reason     TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_stock_movements_stock ON stock_movements(stock_id);

-- 7. Dépenses
CREATE TABLE IF NOT EXISTS expenses (
    id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    farm_id     TEXT NOT NULL REFERENCES farms(id),
    label       TEXT NOT NULL,
    amount      DOUBLE PRECISION NOT NULL DEFAULT 0,
    category    TEXT DEFAULT 'Autre',
    expense_date TIMESTAMPTZ DEFAULT NOW(),
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_expenses_farm ON expenses(farm_id);

-- 8. Produits en vente
CREATE TABLE IF NOT EXISTS products (
    id                TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    short_code        TEXT UNIQUE,
    name              TEXT NOT NULL,
    category_label    TEXT DEFAULT 'Céréales',
    description       TEXT,
    price             DOUBLE PRECISION NOT NULL DEFAULT 0,
    unit              TEXT DEFAULT 'kg',
    quantity_for_sale DOUBLE PRECISION DEFAULT 0,
    images            TEXT[] DEFAULT ARRAY[]::text[],
    local_names       JSONB,
    audio_url         TEXT,
    producer_id       TEXT NOT NULL REFERENCES producers(id),
    is_published      BOOLEAN DEFAULT true,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_products_producer ON products(producer_id);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(LOWER(name));

-- 10. Clients (acheteurs finaux pour un Producteur)
CREATE TABLE IF NOT EXISTS clients (
    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    email TEXT,
    location TEXT,
    total_orders INTEGER DEFAULT 0,
    total_spent DOUBLE PRECISION DEFAULT 0,
    last_order_date TIMESTAMPTZ,
    producer_id TEXT NOT NULL REFERENCES producers(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_clients_phone ON clients(phone);
CREATE INDEX IF NOT EXISTS idx_clients_producer ON clients(producer_id);

-- 11. Agent actions (validation / approval workflow for agent-initiated ops)
CREATE TABLE IF NOT EXISTS agent_actions (
    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    agent_name TEXT NOT NULL,
    action_type TEXT NOT NULL,
    payload JSONB,
    status TEXT DEFAULT 'PENDING' CHECK (status IN ('PENDING','APPROVED','REJECTED','EXECUTED','FAILED')),
    priority TEXT DEFAULT 'MEDIUM' CHECK (priority IN ('LOW','MEDIUM','HIGH','CRITICAL')),
    order_id TEXT UNIQUE,
    user_id TEXT,
    audit_trail_id TEXT,
    ai_reasoning TEXT,
    admin_notes TEXT,
    validated_by_id TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_actions_agent ON agent_actions(agent_name);

-- 9. Commandes
CREATE TABLE IF NOT EXISTS orders (
    id             TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    buyer_id       TEXT REFERENCES users(id),
    client_id      TEXT REFERENCES clients(id),
    customer_phone TEXT,
    customer_name  TEXT,
    zone_id        TEXT REFERENCES zones(id),
    payment_method TEXT DEFAULT 'CASH',
    status         TEXT DEFAULT 'PENDING'
                       CHECK (status IN ('PENDING','CONFIRMED','SHIPPED','DELIVERED','CANCELLED')),
    source         TEXT DEFAULT 'APP',
    whatsapp_id    TEXT,
    is_agent_order BOOLEAN DEFAULT false,
    agent_action_id TEXT REFERENCES agent_actions(id),
    total_amount   DOUBLE PRECISION DEFAULT 0,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at     TIMESTAMPTZ DEFAULT NOW()
);

-- 10. Lignes de commande
CREATE TABLE IF NOT EXISTS order_items (
    id            TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    order_id      TEXT NOT NULL REFERENCES orders(id),
    product_id    TEXT NOT NULL REFERENCES products(id),
    quantity      DOUBLE PRECISION NOT NULL DEFAULT 1,
    price_at_sale DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);

-- 11. Alertes marché (matching acheteur ↔ vendeur)
CREATE TABLE IF NOT EXISTS market_alerts (
    id                     TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    product_name           TEXT NOT NULL,
    zone_id                TEXT REFERENCES zones(id),
    buyer_phone            TEXT,
    status                 TEXT DEFAULT 'SEARCHING'
                               CHECK (status IN ('SEARCHING','MATCHED','EXPIRED')),
    matched_product_id     TEXT REFERENCES products(id),
    matched_producer_phone TEXT,
    created_at             TIMESTAMPTZ DEFAULT NOW(),
    updated_at             TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_market_alerts_product ON market_alerts(LOWER(product_name));
CREATE INDEX IF NOT EXISTS idx_market_alerts_status ON market_alerts(status);

-- ── Extension table users (ajout colonne is_onboarded si absente) ──
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'is_onboarded'
    ) THEN
        ALTER TABLE users ADD COLUMN is_onboarded BOOLEAN DEFAULT false;
    END IF;
END $$;
