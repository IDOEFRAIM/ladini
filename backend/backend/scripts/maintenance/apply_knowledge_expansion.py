import sys
import os
import sqlalchemy
from sqlalchemy import text
from dotenv import load_dotenv

# Add backend to path
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

def apply_migration():
    # Load .env explicitly from backend root
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(base_dir, ".env")
    load_dotenv(env_path, override=True)
    
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("❌ DATABASE_URL not found in .env")
        return

    # Mask password for safety
    safe_url = db_url.split('@')[-1] if '@' in db_url else "Unknown"
    print(f"🔌 Connecting to DB: ...{safe_url}") 
    
    try:
        engine = sqlalchemy.create_engine(db_url)
        
        # Read SQL file
        sql_path = os.path.join(base_dir, "src", "agriconnect", "database", "seed_sesame_sorgho.sql")
        
        print(f"📄 Reading SQL from: {sql_path}")
        with open(sql_path, "r", encoding="utf-8") as f:
            sql_content = f.read()

        print("🚀 Executing expansion...")
        with engine.connect() as conn:
            conn.execute(text(sql_content))
            conn.commit()
            print("✅ Expansion applied successfully (Sésame & Sorgho added).")
            
    except Exception as e:
        print(f"❌ Error applying expansion: {e}")

if __name__ == "__main__":
    apply_migration()