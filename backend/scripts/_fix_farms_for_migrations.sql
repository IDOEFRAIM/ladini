-- Fix farms table for migrations: add id column and primary key if missing
ALTER TABLE IF EXISTS farms ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();

-- Populate any NULL ids (if any)
UPDATE farms SET id = gen_random_uuid() WHERE id IS NULL;

DO $do$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.farms'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE farms ADD CONSTRAINT farms_pkey PRIMARY KEY (id);
    END IF;
END $do$;
