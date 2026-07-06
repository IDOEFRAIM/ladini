-- Migration: ajouter la colonne user_id à conversations (idempotent)
-- Ajoute également un index sur user_id et tente d'ajouter une FK vers users(id) si possible.

BEGIN;

-- 1) Ajouter la colonne user_id si elle n'existe pas (TEXT, pas UUID — les IDs sont des cuid strings)
DO $$
BEGIN
    -- Si la colonne existe en UUID (ancienne migration), convertir en TEXT
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'conversations' AND column_name = 'user_id' AND data_type = 'uuid'
    ) THEN
        ALTER TABLE conversations ALTER COLUMN user_id TYPE TEXT USING user_id::TEXT;
        RAISE NOTICE 'Converted user_id from UUID to TEXT';
    END IF;
END $$;

ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS user_id TEXT;

-- 2) Créer un index sur user_id si manquant
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id);

-- 3) Tenter d'ajouter la contrainte de clé étrangère si elle n'existe pas
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON c.conrelid = t.oid
        WHERE c.contype = 'f' AND t.relname = 'conversations'
    ) THEN
        -- Note: si users.id n'existe pas ou a un type incompatible, cette instruction échouera.
        BEGIN
            ALTER TABLE conversations
                ADD CONSTRAINT fk_conversations_user
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE;
        EXCEPTION WHEN others THEN
            RAISE NOTICE 'FK add skipped: %', SQLERRM;
        END;
    END IF;
END $$;

COMMIT;

-- Fin migration

-- Vérification rapide (affiche le schéma de la table conversations)
-- SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'conversations';
