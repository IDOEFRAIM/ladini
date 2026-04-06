import sys
import os
import sqlalchemy
from sqlalchemy import text
from dotenv import load_dotenv

# Add backend to path
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from agriconnect.core.settings import settings

load_dotenv()

def apply_migration():
    # Load .env explicitly from backend root
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    load_dotenv(env_path)
    
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("❌ DATABASE_URL not found in .env")
        return

    print(f"🔌 Connecting to DB: {db_url.split('@')[-1]}") # Mask password
    engine = sqlalchemy.create_engine(db_url)
    
    # Read SQL file
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sql_path = os.path.join(base_dir, "src", "agriconnect", "database", "migration_create_knowledge_base.sql")
    
    if not os.path.exists(sql_path):
        print(f"❌ SQL file not found at: {sql_path}")
        return

    print(f"📄 Reading SQL from: {sql_path}")
    with open(sql_path, "r", encoding="utf-8") as f:
        sql = f.read()

    with engine.connect() as conn:
        print("🚀 Executing migration...")
        trans = conn.begin()
        try:
            conn.execute(text(sql))
            trans.commit()
            print("✅ Migration applied successfully.")
        except Exception as e:
            trans.rollback()
            print(f"❌ Migration failed: {e}")
            raise

if __name__ == "__main__":
    apply_migration()
