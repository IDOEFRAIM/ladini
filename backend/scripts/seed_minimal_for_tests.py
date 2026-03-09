import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agriconnect.core.settings import settings
import psycopg2
import uuid

DDL = '''
-- Minimal schema to satisfy integration tests
CREATE SCHEMA IF NOT EXISTS auth;
CREATE SCHEMA IF NOT EXISTS governance;
CREATE SCHEMA IF NOT EXISTS marketplace;

-- Try to create simple tables only if they do not exist.
CREATE TABLE IF NOT EXISTS marketplace.transaction_staging (
    id TEXT PRIMARY KEY,
    transaction_id TEXT UNIQUE NOT NULL,
    payload JSONB,
    status TEXT DEFAULT 'PENDING',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);
'''


def main():
    db_url = settings.DATABASE_URL
    if not db_url:
        print('ERROR: DATABASE_URL not configured')
        sys.exit(1)

    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    cur = conn.cursor()
    try:
        # Apply minimal DDL (safe)
        try:
            cur.execute(DDL)
        except Exception as e:
            print('Warning: DDL step failed (continuing):', e)

        # Generate UUIDs to match existing UUID typed PKs if present
        user_id = str(uuid.uuid4())
        producer_id = str(uuid.uuid4())
        product_id = str(uuid.uuid4())
        stock_id = str(uuid.uuid4())

        # Clean up legacy non-UUID test rows that break typed comparisons
        try:
            cur.execute("DELETE FROM marketplace.products WHERE id::text = 'test-product-1'")
            cur.execute("DELETE FROM marketplace.stocks WHERE id::text = 'stock-1'")
            cur.execute("DELETE FROM auth.users WHERE id::text = 'test-user-1'")
            cur.execute("DELETE FROM marketplace.producers WHERE id::text = 'test-producer-1'")
            # Also clean up legacy public-schema tables created earlier without namespaces
            try:
                cur.execute("DELETE FROM products WHERE id::text = 'test-product-1'")
                cur.execute("DELETE FROM stocks WHERE id::text = 'stock-1'")
                cur.execute("DELETE FROM users WHERE id::text = 'test-user-1'")
                cur.execute("DELETE FROM producers WHERE id::text = 'test-producer-1'")
            except Exception:
                # ignore if public tables do not exist
                pass
        except Exception as e:
            print('Warning: cleanup of legacy test rows failed (continuing):', e)

        # Insert user into auth.users; tolerate different schemas via TRY
        try:
            cur.execute(
                """
                INSERT INTO auth.users (id, phone, name)
                VALUES (%s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (user_id, '+22670000001', 'Test User'),
            )
        except Exception as e:
            print('Warning: insert into auth.users failed (continuing):', e)

        # Insert producer referencing auth.users
        try:
            cur.execute(
                """
                INSERT INTO marketplace.producers (id, user_id)
                VALUES (%s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (producer_id, user_id),
            )
        except Exception as e:
            print('Warning: insert into marketplace.producers failed (continuing):', e)

        # Insert product
        try:
            cur.execute(
                """
                INSERT INTO marketplace.products (id, name, price, quantity_for_sale, producer_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (product_id, 'Test Grain', 1000.0, 10, producer_id),
            )
        except Exception as e:
            print('Warning: insert into marketplace.products failed (continuing):', e)

        # Insert stock
        try:
            cur.execute(
                """
                INSERT INTO marketplace.stocks (id, farm_id, item_name, quantity, unit)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (stock_id, 'farm-1', 'Test Grain', 100, 'kg'),
            )
        except Exception as e:
            print('Warning: insert into marketplace.stocks failed (continuing):', e)

        print('✅ Minimal schema and seeds applied (partial - some inserts may have been skipped)')
    except Exception as e:
        print('ERROR applying minimal seed:', e)
    finally:
        cur.close()
        conn.close()


if __name__ == '__main__':
    main()
