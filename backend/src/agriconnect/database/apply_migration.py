"""
Quick migration script: fix user_id column on conversations table.
Converts UUID→TEXT if needed, adds column if missing, creates index and FK.
"""
import sys
from sqlalchemy import text

from agriconnect.core.db import get_engine, resolve_database_url

def main():
    db_url = resolve_database_url(required=True)
    engine = get_engine(db_url)

    with engine.begin() as conn:
        # 1. Check current state of user_id column
        row = conn.execute(
            text(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = 'conversations' AND column_name = 'user_id'
                """
            )
        ).first()

        if row:
            print(f"Column user_id exists: type={row[1]}")
            if row[1] == "uuid":
                print("Converting UUID -> TEXT...")
                conn.execute(text("ALTER TABLE conversations ALTER COLUMN user_id TYPE TEXT USING user_id::TEXT"))
                print("✅ Converted to TEXT")
            else:
                print("✅ Already TEXT, no change needed")
        else:
            print("Adding user_id column as TEXT...")
            conn.execute(text("ALTER TABLE conversations ADD COLUMN user_id TEXT"))
            print("✅ Added user_id TEXT column")

        # 2. Create index if missing
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id)"))
        print("✅ Index ensured")

        # 3. Try FK if possible
        try:
            conn.execute(
                text(
                    """
                    DO $$ BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint WHERE conname = 'fk_conversations_user'
                        ) THEN
                            ALTER TABLE conversations ADD CONSTRAINT fk_conversations_user
                                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE;
                        END IF;
                    END $$;
                    """
                )
            )
            print("✅ FK ensured")
        except Exception as e:
            print(f"⚠️  FK skipped: {e}")

        # 4. Show final schema
        rows = conn.execute(
            text(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_name = 'conversations'
                ORDER BY ordinal_position
                """
            )
        ).fetchall()
        print("\n=== conversations schema ===")
        for r in rows:
            print(f"  {r[0]:30s} {r[1]:15s} nullable={r[2]}")

    print("\n✅ Migration complete")


if __name__ == "__main__":
    main()
