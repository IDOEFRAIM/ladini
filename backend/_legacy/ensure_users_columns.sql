-- Ensure commonly-expected user columns exist (nullable, non-destructive)
-- Run this against your Postgres database (psql or any SQL client).
-- It will add nullable columns only if they do not already exist.

-- Top-level users table (common Prisma layout)
ALTER TABLE IF EXISTS users
  ADD COLUMN IF NOT EXISTS "role" VARCHAR,
  ADD COLUMN IF NOT EXISTS "zone_id" UUID,
  ADD COLUMN IF NOT EXISTS "zoneId" UUID,
  ADD COLUMN IF NOT EXISTS "image" VARCHAR,
  ADD COLUMN IF NOT EXISTS "created_at" TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS "updated_at" TIMESTAMPTZ;

-- auth schema (models_v3 uses auth.users)
ALTER TABLE IF EXISTS auth.users
  ADD COLUMN IF NOT EXISTS "role" VARCHAR,
  ADD COLUMN IF NOT EXISTS "zone_id" UUID,
  ADD COLUMN IF NOT EXISTS "zoneId" UUID,
  ADD COLUMN IF NOT EXISTS "image" VARCHAR,
  ADD COLUMN IF NOT EXISTS "created_at" TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS "updated_at" TIMESTAMPTZ;

-- CamelCase variants (Prisma sometimes uses createdAt/updatedAt)
ALTER TABLE IF EXISTS users
  ADD COLUMN IF NOT EXISTS "createdAt" TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS "updatedAt" TIMESTAMPTZ;

ALTER TABLE IF EXISTS auth.users
  ADD COLUMN IF NOT EXISTS "createdAt" TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS "updatedAt" TIMESTAMPTZ;

-- Notes:
-- - All new columns are nullable to avoid interfering with existing data.
-- - Review and adjust types/constraints according to your Prisma schema before applying in production.
