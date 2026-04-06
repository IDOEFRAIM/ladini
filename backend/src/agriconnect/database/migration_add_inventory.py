"""
Migration: add inventory tables for seed allocations/distributions/attempts.
Run: python backend/src/agriconnect/database/migration_add_inventory.py
"""
import sys
from sqlalchemy import text

from agriconnect.core.db import get_engine, resolve_database_url

SQL = """
CREATE SCHEMA IF NOT EXISTS marketplace;

CREATE TABLE IF NOT EXISTS marketplace.seed_allocations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES governance.organizations(id),
  zone_id uuid NOT NULL REFERENCES governance.zones(id),
  seed_type text NOT NULL,
  total_quantity integer NOT NULL,
  remaining_quantity integer NOT NULL,
  unit text NOT NULL DEFAULT 'KG',
  allocated_by_id uuid REFERENCES auth.users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_seed_allocations_org ON marketplace.seed_allocations(organization_id);
CREATE INDEX IF NOT EXISTS idx_seed_allocations_zone ON marketplace.seed_allocations(zone_id);
CREATE INDEX IF NOT EXISTS idx_seed_allocations_seedtype ON marketplace.seed_allocations(seed_type);

CREATE TABLE IF NOT EXISTS marketplace.seed_distributions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  allocation_id uuid NOT NULL REFERENCES marketplace.seed_allocations(id),
  producer_id uuid NOT NULL REFERENCES marketplace.producers(id),
  agent_id uuid NOT NULL REFERENCES auth.users(id),
  organization_id uuid NOT NULL REFERENCES governance.organizations(id),
  zone_id uuid NOT NULL REFERENCES governance.zones(id),
  quantity integer NOT NULL,
  cnib_provided text,
  verification_code_hash text,
  verification_code_expires_at timestamptz,
  verification_channel text NOT NULL DEFAULT 'IN_APP',
  attempts_count integer NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'PENDING',
  receipt_at timestamptz,
  metadata jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_seed_distributions_alloc ON marketplace.seed_distributions(allocation_id);
CREATE INDEX IF NOT EXISTS idx_seed_distributions_producer ON marketplace.seed_distributions(producer_id);
CREATE INDEX IF NOT EXISTS idx_seed_distributions_agent ON marketplace.seed_distributions(agent_id);
CREATE INDEX IF NOT EXISTS idx_seed_distributions_status ON marketplace.seed_distributions(status);

CREATE TABLE IF NOT EXISTS marketplace.seed_distribution_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  distribution_id uuid NOT NULL REFERENCES marketplace.seed_distributions(id),
  actor_id uuid REFERENCES auth.users(id),
  attempt_type text,
  success boolean NOT NULL DEFAULT false,
  ip_address text,
  metadata jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sda_distribution ON marketplace.seed_distribution_attempts(distribution_id);
CREATE INDEX IF NOT EXISTS idx_sda_actor ON marketplace.seed_distribution_attempts(actor_id);
"""


def main():
    db_url = resolve_database_url(required=True)
    engine = get_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.execute(text(SQL))
        print("✅ Inventory tables created/ensured")
    except Exception as e:
        print("❌ Migration failed:", e)
        sys.exit(1)


if __name__ == '__main__':
    main()
