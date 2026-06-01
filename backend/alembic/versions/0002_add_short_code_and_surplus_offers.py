"""Add products.short_code and surplus_offers.

This migration fixes runtime mismatches between the canonical ORM in
`agriconnect.domain.models` and the actual database schema.
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "0002_short_code_surplus"
down_revision = "0001_initial_from_models_v3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add missing column used by Product ORM.
    op.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS short_code text")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS products_short_code_uidx "
        "ON products (short_code) WHERE short_code IS NOT NULL"
    )

    # Table used by MarketplaceMixin.create_surplus_offer.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS surplus_offers (
            id uuid PRIMARY KEY,
            user_id text,
            product_name text NOT NULL,
            quantity_kg double precision NOT NULL,
            price_kg double precision,
            zone_id uuid,
            location text,
            channel text,
            created_at timestamp DEFAULT now() NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS surplus_offers")
    op.execute("DROP INDEX IF EXISTS products_short_code_uidx")
    op.execute("ALTER TABLE products DROP COLUMN IF EXISTS short_code")
