
import sys
from pathlib import Path
# Add project source to path (3 levels up from <category>/script.py -> backend/src)
sys.path.append(str(Path(__file__).resolve().parents[2] / "src"))

"""
Vérifie la présence des tables 'climatic_regions' et 'zones' dans la base.
Affiche aussi le schéma de la table zones.
"""
import os
from sqlalchemy import text
from agriconnect.core.db import get_engine, resolve_database_url

try:
    from agriconnect.core.settings import settings
    db_url = settings.DATABASE_URL or resolve_database_url(required=True)
except Exception:
    db_url = os.environ.get("DATABASE_URL") or resolve_database_url(required=True)

if not db_url:
    raise RuntimeError("DATABASE_URL introuvable (dans .env ou settings.py)")

engine = get_engine(db_url)
with engine.connect() as conn:
    print("Tables présentes dans la base :")
    res = conn.execute(text("""
        SELECT table_schema, table_name FROM information_schema.tables
        WHERE table_name IN ('climatic_regions', 'zones')
    """)).fetchall()
    for row in res:
        print(f"- {row.table_schema}.{row.table_name}")
    if not res:
        print("Aucune des deux tables n'existe !")

    print("\nColonnes de zones :")
    res2 = conn.execute(text("""
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_name = 'zones'
    """)).fetchall()
    for row in res2:
        print(f"- {row.column_name} ({row.data_type})")
    if not res2:
        print("Table zones absente ou sans colonnes.")
