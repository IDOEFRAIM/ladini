
import sys
from pathlib import Path
# Add project source to path (3 levels up from <category>/script.py -> backend/src)
sys.path.append(str(Path(__file__).resolve().parents[2] / "src"))

"""
Patch les tables existantes pour ajouter les colonnes manquantes :
- climatic_region_id dans zones
- is_onboarded dans users

Utilise DATABASE_URL du .env ou settings.py.
"""
import os
from sqlalchemy import create_engine, text

try:
    from agriconnect.core.settings import settings
    db_url = settings.DATABASE_URL
except Exception:
    db_url = os.environ.get("DATABASE_URL")

if not db_url:
    raise RuntimeError("DATABASE_URL introuvable (dans .env ou settings.py)")

from sqlalchemy.exc import ProgrammingError
engine = create_engine(db_url, pool_pre_ping=True)

# 1. Ajout climatic_region_id à zones si absent
with engine.connect() as conn:
    res = conn.execute(text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'zones' AND column_name = 'climatic_region_id'
    """)).fetchone()
if not res:
    print("Ajout de climatic_region_id à zones...")
    try:
        with engine.begin() as conn2:
            conn2.execute(text("ALTER TABLE zones ADD COLUMN climatic_region_id TEXT REFERENCES climatic_regions(id)"))
        print("✅ Colonne climatic_region_id ajoutée à zones.")
    except ProgrammingError as e:
        print(f"❌ Erreur ajout climatic_region_id: {e}")
    except Exception as e:
        print(f"❌ Erreur inattendue: {e}")
else:
    print("Colonne climatic_region_id déjà présente dans zones.")

# 2. Ajout is_onboarded à users si absent
with engine.connect() as conn:
    res = conn.execute(text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'is_onboarded'
    """)).fetchone()
if not res:
    print("Ajout de is_onboarded à users...")
    try:
        with engine.begin() as conn2:
            conn2.execute(text("ALTER TABLE users ADD COLUMN is_onboarded BOOLEAN DEFAULT false"))
        print("✅ Colonne is_onboarded ajoutée à users.")
    except ProgrammingError as e:
        print(f"❌ Erreur ajout is_onboarded: {e}")
    except Exception as e:
        print(f"❌ Erreur inattendue: {e}")
else:
    print("Colonne is_onboarded déjà présente dans users.")
