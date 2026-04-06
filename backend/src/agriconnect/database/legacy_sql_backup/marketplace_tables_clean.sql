-- marketplace_tables_clean.sql : version compatible SQLAlchemy/psycopg2
-- NE PAS inclure de DO $$ ... ni de IF NOT EXISTS sur ALTER TABLE

CREATE TABLE IF NOT EXISTS climatic_regions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS producers (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id),
    zone_id    UUID REFERENCES zones(id),
    status     TEXT DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','INACTIVE','SUSPENDED')),
    bio        TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_producers_user ON producers(user_id);

CREATE TABLE IF NOT EXISTS farms (
    producer_id UUID NOT NULL REFERENCES producers(id),
    zone_id     UUID REFERENCES zones(id),
    latitude    DOUBLE PRECISION,
    longitude   DOUBLE PRECISION,
    size_ha     DOUBLE PRECISION,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='farms' AND column_name='producer_id'
    ) THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_farms_producer ON ' || quote_ident(current_schema()) || '.farms(' || quote_ident('producer_id') || ')';
    ELSIF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='farms' AND column_name='producerId'
    ) THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_farms_producer ON ' || quote_ident(current_schema()) || '.farms(' || quote_ident('producerId') || ')';
    ELSIF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='farms' AND column_name='prodducer_id'
    ) THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_farms_producer ON ' || quote_ident(current_schema()) || '.farms(' || quote_ident('prodducer_id') || ')';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS stocks (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    farm_id    UUID NOT NULL REFERENCES farms(id),
    item_name  TEXT NOT NULL,
    quantity   DOUBLE PRECISION DEFAULT 0,
    unit       TEXT DEFAULT 'kg',
    type       TEXT DEFAULT 'HARVEST' CHECK (type IN ('HARVEST','INPUT','OTHER')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='stocks' AND column_name='farm_id'
    ) THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_stocks_farm ON ' || quote_ident(current_schema()) || '.stocks(' || quote_ident('farm_id') || ')';
    ELSIF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='stocks' AND column_name='farmId'
    ) THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_stocks_farm ON ' || quote_ident(current_schema()) || '.stocks(' || quote_ident('farmId') || ')';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS stock_movements (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stock_id   UUID NOT NULL REFERENCES stocks(id),
    type       TEXT NOT NULL CHECK (type IN ('IN','OUT','ADJUSTMENT')),
    quantity   DOUBLE PRECISION NOT NULL,
    reason     TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_stock_movements_stock ON stock_movements(stock_id);

CREATE TABLE IF NOT EXISTS expenses (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    farm_id     UUID NOT NULL REFERENCES farms(id),
    label       TEXT NOT NULL,
    amount      DOUBLE PRECISION NOT NULL DEFAULT 0,
    category    TEXT DEFAULT 'Autre',
    expense_date TIMESTAMPTZ DEFAULT NOW(),
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='expenses' AND column_name='farm_id'
    ) THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_expenses_farm ON ' || quote_ident(current_schema()) || '.expenses(' || quote_ident('farm_id') || ')';
    ELSIF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='expenses' AND column_name='farmId'
    ) THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_expenses_farm ON ' || quote_ident(current_schema()) || '.expenses(' || quote_ident('farmId') || ')';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS products (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    short_code        TEXT UNIQUE,
    name              TEXT NOT NULL,
    category_label    TEXT DEFAULT 'Céréales',
    description       TEXT,
    price             DOUBLE PRECISION NOT NULL DEFAULT 0,
    unit              TEXT DEFAULT 'kg',
    quantity_for_sale DOUBLE PRECISION DEFAULT 0,
    producer_id       UUID NOT NULL REFERENCES producers(id),
    is_published      BOOLEAN DEFAULT true,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='products' AND column_name='producer_id'
    ) THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_products_producer ON ' || quote_ident(current_schema()) || '.products(' || quote_ident('producer_id') || ')';
    ELSIF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='products' AND column_name='producerId'
    ) THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_products_producer ON ' || quote_ident(current_schema()) || '.products(' || quote_ident('producerId') || ')';
    ELSIF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name='products' AND column_name='prodducer_id'
    ) THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_products_producer ON ' || quote_ident(current_schema()) || '.products(' || quote_ident('prodducer_id') || ')';
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_products_name ON products(LOWER(name));

CREATE TABLE IF NOT EXISTS orders (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_phone TEXT,
    customer_name  TEXT,
    zone_id        UUID REFERENCES zones(id),
    status         TEXT DEFAULT 'PENDING'
                       CHECK (status IN ('PENDING','CONFIRMED','SHIPPED','DELIVERED','CANCELLED')),
    source         TEXT DEFAULT 'WHATSAPP',
    total_amount   DOUBLE PRECISION DEFAULT 0,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Ensure `producer_id` exists on orders for compatibility with older schemas
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name='orders'
    ) THEN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() AND table_name='orders' AND column_name='producer_id'
        ) THEN
            ALTER TABLE public.orders ADD COLUMN producer_id UUID;
        END IF;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS order_items (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id      UUID NOT NULL REFERENCES orders(id),
    product_id    UUID NOT NULL REFERENCES products(id),
    quantity      DOUBLE PRECISION NOT NULL DEFAULT 1,
    price_at_sale DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);

CREATE TABLE IF NOT EXISTS market_alerts (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_name           TEXT NOT NULL,
    zone_id                UUID REFERENCES zones(id),
    buyer_phone            TEXT,
    status                 TEXT DEFAULT 'SEARCHING'
                               CHECK (status IN ('SEARCHING','MATCHED','EXPIRED')),
    matched_product_id     UUID REFERENCES products(id),
    matched_producer_phone TEXT,
    created_at             TIMESTAMPTZ DEFAULT NOW(),
    updated_at             TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_market_alerts_product ON market_alerts(LOWER(product_name));
CREATE INDEX IF NOT EXISTS idx_market_alerts_status ON market_alerts(status);
