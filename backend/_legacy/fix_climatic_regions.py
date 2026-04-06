
import sys
from pathlib import Path
# Add project source to path (3 levels up from <category>/script.py -> backend/src)
sys.path.append(str(Path(__file__).resolve().parents[2] / "src"))

"""
Crée la table climatic_regions si absente, puis ajoute la colonne climatic_region_id à zones si absente.
"""
import os
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

try:
    from agriconnect.core.settings import settings
    db_url = settings.DATABASE_URL
except Exception:
    db_url = os.environ.get("DATABASE_URL")

if not db_url:
    raise RuntimeError("DATABASE_URL introuvable (dans .env ou settings.py)")

engine = create_engine(db_url, pool_pre_ping=True)

# 1. Créer la table climatic_regions si absente
with engine.begin() as conn:
    try:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS climatic_regions (
                id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
                name        TEXT NOT NULL UNIQUE,
                description TEXT,
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                updated_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        print("✅ Table climatic_regions OK.")
    except Exception as e:
        print(f"❌ Erreur création climatic_regions: {e}")

# 2. Ajouter la colonne climatic_region_id à zones si absente
with engine.connect() as conn:
    res = conn.execute(text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'zones' AND column_name = 'climatic_region_id'
    """)).fetchone()
if not res:
    print("Ajout de climatic_region_id à zones...")
    try:
        with engine.begin() as conn2:
            conn2.execute(text("ALTER TABLE zones ADD COLUMN climatic_region_id TEXT REFERENCES public.climatic_regions(id)"))
        print("✅ Colonne climatic_region_id ajoutée à zones.")
    except ProgrammingError as e:
        print(f"❌ Erreur ajout climatic_region_id: {e}")
    except Exception as e:
        print(f"❌ Erreur inattendue: {e}")
else:
    print("Colonne climatic_region_id déjà présente dans zones.")
