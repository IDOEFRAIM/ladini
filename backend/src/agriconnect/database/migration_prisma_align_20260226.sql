-- Migration: Align SQL schema with frontend Prisma schema
-- Generated: 2026-02-26

SET search_path TO public;

-- 1) USERS: add auth fields if missing
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='users' AND column_name='email'
  ) THEN
    ALTER TABLE users ADD COLUMN email VARCHAR(255) UNIQUE;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='users' AND column_name='password'
  ) THEN
    ALTER TABLE users ADD COLUMN password TEXT;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='users' AND column_name='email_verified'
  ) THEN
    ALTER TABLE users ADD COLUMN email_verified TIMESTAMP;
  END IF;
END $$;

-- 2) Authentication tables (NextAuth-like)
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

-- 3) Producers status: normalize values and add check constraint
UPDATE producers SET status='PENDING' WHERE status='INACTIVE';
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE constraint_name='chk_producers_status' AND table_name='producers'
  ) THEN
    ALTER TABLE producers ADD CONSTRAINT chk_producers_status CHECK (status IN ('PENDING','ACTIVE','SUSPENDED'));
  END IF;
END $$;

-- 4) Stocks: map OTHER -> EQUIPMENT and ensure constraint
UPDATE stocks SET type='EQUIPMENT' WHERE type='OTHER';
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE constraint_name='chk_stocks_type' AND table_name='stocks'
  ) THEN
    ALTER TABLE stocks ADD CONSTRAINT chk_stocks_type CHECK (type IN ('HARVEST','INPUT','EQUIPMENT'));
  END IF;
END $$;

-- 5) Stock movements: map ADJUSTMENT -> WASTE and ensure constraint
UPDATE stock_movements SET type='WASTE' WHERE type='ADJUSTMENT';
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE constraint_name='chk_stock_movements_type' AND table_name='stock_movements'
  ) THEN
    ALTER TABLE stock_movements ADD CONSTRAINT chk_stock_movements_type CHECK (type IN ('IN','OUT','WASTE'));
  END IF;
END $$;

-- 6) Products: add images, local_names, audio_url if missing
ALTER TABLE products ADD COLUMN IF NOT EXISTS images TEXT[] DEFAULT ARRAY[]::text[];
ALTER TABLE products ADD COLUMN IF NOT EXISTS local_names JSONB;
ALTER TABLE products ADD COLUMN IF NOT EXISTS audio_url TEXT;

-- 7) Clients table (safe create)
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

-- 8) Agent actions table
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

-- 9) Orders: add optional buyer/client/payment & agent linkage
ALTER TABLE orders ADD COLUMN IF NOT EXISTS buyer_id TEXT REFERENCES users(id);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS client_id TEXT REFERENCES clients(id);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_method TEXT DEFAULT 'CASH';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS whatsapp_id TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS is_agent_order BOOLEAN DEFAULT false;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS agent_action_id TEXT REFERENCES agent_actions(id);
-- set source default to APP if column exists
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='orders' AND column_name='source') THEN
    ALTER TABLE orders ALTER COLUMN source SET DEFAULT 'APP';
  END IF;
END $$;

-- 10) Ensure user_farm_profiles and episodic_memories exist (no-op if already)
CREATE TABLE IF NOT EXISTS user_farm_profiles (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id VARCHAR(255) UNIQUE NOT NULL,
  profile_data JSONB NOT NULL DEFAULT '{}'::jsonb,
  version VARCHAR(10) DEFAULT '1',
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS episodic_memories (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id VARCHAR(255) NOT NULL,
  summary TEXT NOT NULL,
  category VARCHAR(100),
  relevance_score DOUBLE PRECISION DEFAULT 0,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 11) Protocol logs and audit tables exist in protocol_tables.sql / audit_trail.sql
-- (no changes here)

-- Migration completed

-- Simple verification selects (print results when running interactively)
-- SELECT column_name FROM information_schema.columns WHERE table_name='users' AND column_name IN ('email','password','email_verified');
-- SELECT to_regclass('public.clients'), to_regclass('public.agent_actions');


COMMIT;
