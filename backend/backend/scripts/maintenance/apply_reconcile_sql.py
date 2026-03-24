import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agriconnect.core.settings import settings
import psycopg2


SQL_STATEMENTS = [
    # Ensure accounts/sessions exist in public and reference auth.users (UUID)
    '''
    CREATE TABLE IF NOT EXISTS public.accounts (
      id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
      user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
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
    CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_provider ON public.accounts(provider, provider_account_id);
    ''',

    '''
    CREATE TABLE IF NOT EXISTS public.sessions (
      id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
      session_token VARCHAR(255) UNIQUE NOT NULL,
      user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
      expires TIMESTAMP NOT NULL,
      created_at TIMESTAMP DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_user ON public.sessions(user_id);
    ''',

    # Clients: create if missing; if existing with camelCase columns, adapt indexes
    '''
    CREATE TABLE IF NOT EXISTS public.clients (
      id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
      name TEXT NOT NULL,
      phone TEXT NOT NULL,
      email TEXT,
      location TEXT,
      total_orders INTEGER DEFAULT 0,
      total_spent DOUBLE PRECISION DEFAULT 0,
      last_order_date TIMESTAMPTZ,
      producer_id UUID NOT NULL REFERENCES marketplace.producers(id),
      created_at TIMESTAMPTZ DEFAULT NOW(),
      updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    -- If the table already existed with camelCase columns (producerId), add missing column safely
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='clients' AND column_name='producerId') THEN
        IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname='public' AND tablename='clients' AND indexname='idx_clients_producer') THEN
          CREATE INDEX IF NOT EXISTS idx_clients_producer ON public.clients("producerId");
        END IF;
      ELSE
        CREATE INDEX IF NOT EXISTS idx_clients_producer ON public.clients(producer_id);
      END IF;
    END $$;
    CREATE INDEX IF NOT EXISTS idx_clients_phone ON public.clients(phone);
    ''',

    # Orders: add buyer/client/payment/agent columns with UUIDs where appropriate
    '''
    -- Add order-related columns using TEXT for compatibility with existing public.orders schema
    ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS buyer_id TEXT;
    ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS client_id TEXT;
    ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS payment_method TEXT DEFAULT 'CASH';
    ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS whatsapp_id TEXT;
    ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS is_agent_order BOOLEAN DEFAULT false;
    ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS agent_action_id TEXT;
    ''',
      # Ensure public.producers has expected columns used by later migrations
      '''
      ALTER TABLE public.producers ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'ACTIVE';
      -- zones.id in this DB is varchar in public schema; keep compatible TEXT to avoid FK type conflicts
      ALTER TABLE public.producers ADD COLUMN IF NOT EXISTS zone_id TEXT;
      ALTER TABLE public.producers ADD COLUMN IF NOT EXISTS bio TEXT;
      ALTER TABLE public.producers ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
      ALTER TABLE public.producers ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
      ''',
      # Add commonly-missing marketplace/product/stock columns expected by migrations/tests
      '''
      ALTER TABLE public.stocks ADD COLUMN IF NOT EXISTS type TEXT DEFAULT 'HARVEST';
      ALTER TABLE public.products ADD COLUMN IF NOT EXISTS unit TEXT DEFAULT 'kg';
      ALTER TABLE public.products ADD COLUMN IF NOT EXISTS images TEXT[] DEFAULT ARRAY[]::text[];
      ALTER TABLE public.products ADD COLUMN IF NOT EXISTS local_names JSONB;
      ALTER TABLE public.products ADD COLUMN IF NOT EXISTS audio_url TEXT;
      ''',
]


def main():
    db_url = settings.DATABASE_URL
    if not db_url:
        print('ERROR: DATABASE_URL not configured')
        sys.exit(1)

    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    cur = conn.cursor()

    for sql in SQL_STATEMENTS:
        try:
            print('Executing statement block...')
            cur.execute(sql)
            print('  ✅ OK')
        except Exception as e:
            print('  ⚠️ Error executing reconciliation SQL:', e)

    cur.close()
    conn.close()


if __name__ == '__main__':
    main()
