import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agriconnect.core.settings import settings
import psycopg2
from datetime import datetime

# Use the IDs provided in tests
PRODUCER_ID = 'fa987f63-fafa-4147-9676-52c9af0edc75'
ADMIN_ID = 'a0f5c3f2-53d3-420c-b34d-2843db275613'
CLIENT_ID = 'fa987f63-fafa-4147-9676-52c9af0edc75'

DB = settings.DATABASE_URL
if not DB:
    print('ERROR: DATABASE_URL not set')
    sys.exit(1)

now = datetime.utcnow()
conn = psycopg2.connect(DB)
conn.autocommit = True
cur = conn.cursor()
try:
    # Create auth user (producer user) if missing; avoid phone-unique conflicts
    cur.execute(
        """
        INSERT INTO auth.users (id, phone, name)
        VALUES (%s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (CLIENT_ID, '+22607000000', 'Test Client'),
    )

    # Create producer linked to that user if none exists for that user_id
    cur.execute("SELECT id FROM marketplace.producers WHERE user_id = %s", (CLIENT_ID,))
    if not cur.fetchone():
        cur.execute(
            """
            INSERT INTO marketplace.producers (id, user_id)
            VALUES (%s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (PRODUCER_ID, CLIENT_ID),
        )

    # Insert a product with required NOT NULL fields
    cur.execute(
        """
        INSERT INTO marketplace.products (id, short_code, name, category_label, price, unit, quantity_for_sale, images, producer_id, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
        ON CONFLICT (id) DO UPDATE SET quantity_for_sale = EXCLUDED.quantity_for_sale, price = EXCLUDED.price, updated_at = NOW()
        """,
        (
            '00000000-0000-4000-8000-000000000001',
            'TEST1',
            'Test Grain Seed',
            'Céréales',
            1000.0,
            'KG',
            100.0,
            '{}',
            PRODUCER_ID,
        ),
    )

    # Optionally, create a stock row for farm to support stock tests
    cur.execute(
        """
        INSERT INTO marketplace.stocks (id, farm_id, item_name, quantity, unit, type, created_at, updated_at)
        VALUES (%s, NULL, %s, %s, %s, %s, NOW(), NOW())
        ON CONFLICT (id) DO UPDATE SET quantity = EXCLUDED.quantity, updated_at = NOW()
        """,
        ('00000000-0000-4000-8000-000000000002', 'Test Grain Seed', 100.0, 'KG', 'HARVEST'),
    )

    print('✅ Seeded test user/producer/product/stock (ids used)')
except Exception as e:
    print('ERROR seeding test ids:', type(e).__name__, e)
finally:
    cur.close()
    conn.close()
