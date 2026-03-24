ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_user_id_fkey;
ALTER TABLE conversations ALTER COLUMN user_id TYPE TEXT USING user_id::text;
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id);
