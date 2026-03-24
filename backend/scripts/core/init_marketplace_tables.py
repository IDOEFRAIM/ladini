
import sys
from pathlib import Path
# Add project source to path (3 levels up from <category>/script.py -> backend/src)
sys.path.append(str(Path(__file__).resolve().parents[2] / "src"))

"""
Exécute le script SQL marketplace.sql sur la base PostgreSQL (DigitalOcean).
Utilise DATABASE_URL du .env ou settings.py.
"""
import os
from sqlalchemy import create_engine, text

SQL_PATH = "marketplace_tables_clean.sql"

# 1. Récupérer l'URL de la base
try:
    from agriconnect.core.settings import settings
    db_url = settings.DATABASE_URL
except Exception:
    db_url = os.environ.get("DATABASE_URL")

if not db_url:
    raise RuntimeError("DATABASE_URL introuvable (dans .env ou settings.py)")

# 2. Lire le SQL
with open(SQL_PATH, encoding="utf-8") as f:
    sql = f.read()

# 3. Exécuter le SQL (split sur ';' pour compatibilité SQLAlchemy)
engine = create_engine(db_url, pool_pre_ping=True)
with engine.connect() as conn:
    print(f"Connexion OK : {db_url.split('@')[-1].split('?')[0]}")
    for statement in [s.strip() for s in sql.split(';') if s.strip()]:
        try:
            conn.execute(text(statement))
        except Exception as e:
            print(f"⚠️  Erreur sur : {statement[:60]}...\n{e}")
    print("✅ Script marketplace_tables_clean.sql appliqué.")
