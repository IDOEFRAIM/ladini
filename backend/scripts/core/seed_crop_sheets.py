
import sys
from pathlib import Path
# Add project source to path (3 levels up from <category>/script.py -> backend/src)
sys.path.append(str(Path(__file__).resolve().parents[2] / "src"))

"""Seed script: insert sample crop technical sheets into `crop_tech_sheets`.

Usage:
  python -m agriconnect.scripts.seed_crop_sheets

The script uses SQLAlchemy's engine to run INSERT ... ON CONFLICT DO NOTHING.
"""
import os
import json
from sqlalchemy import create_engine
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost:5432/agriconnect")

engine = create_engine(DATABASE_URL)

SAMPLES = [
    {
        "crop_name": "Maïs",
        "lifecycle": {"sowing_days": 7, "vegetative_days": 50, "reproductive_days": 30},
        "needs": {"nitrogen_kg_per_ha": 120, "phosphorus_kg_per_ha": 40, "water_mm": 500},
        "seasonality": {"sowing_months": [4,5], "harvest_months": [9,10]},
        "common_errors": ["Semis trop profond", "Fertilisation insuffisante"],
        "recommendations": {"sowing_depth_cm": 3, "spacing_cm": 50},
        "sources": ["INERA", "Guide local"]
    },
    {
        "crop_name": "Niébé",
        "lifecycle": {"sowing_days": 10, "cycle_days": 75},
        "needs": {"nitrogen_kg_per_ha": 20, "water_mm": 350},
        "seasonality": {"sowing_months": [6,7], "harvest_months": [9]},
        "common_errors": ["Mauvaise densité de semis"],
        "recommendations": {"spacing_cm": 20, "inoculation": True},
        "sources": ["INERA"]
    },
    {
        "crop_name": "Sorgho",
        "lifecycle": {"sowing_days": 8, "cycle_days": 110},
        "needs": {"nitrogen_kg_per_ha": 60, "water_mm": 420},
        "seasonality": {"sowing_months": [4,5], "harvest_months": [10,11]},
        "common_errors": ["Trop d'arrosage initial"],
        "recommendations": {"spacing_cm": 40},
        "sources": ["Guide local"]
    }
]


def upsert_sample(conn, s):
    # Use raw DBAPI cursor to avoid SQLAlchemy named-parameter/style mismatches
    sql = (
        "INSERT INTO crop_tech_sheets (crop_name, lifecycle, needs, seasonality, common_errors, recommendations, sources)"
        " VALUES (%s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb)"
        " ON CONFLICT (crop_name) DO NOTHING"
        " RETURNING id"
    )
    params = (
        s["crop_name"],
        json.dumps(s["lifecycle"]),
        json.dumps(s["needs"]),
        json.dumps(s["seasonality"]),
        json.dumps(s["common_errors"]),
        json.dumps(s["recommendations"]),
        json.dumps(s["sources"]),
    )
    try:
        # engine.raw_connection gives a DBAPI connection (psycopg2) for cursor.execute with %s
        raw = conn.connection
        cur = raw.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        raw.commit()
        if row:
            print(f"Inserted/kept: {s['crop_name']} (id={row[0]})")
        else:
            print(f"Upsert executed for {s['crop_name']}")
        cur.close()
    except Exception as e:
        print(f"Error inserting {s['crop_name']}: {e}")


def main():
    print("Connecting to DB and inserting sample crop tech sheets...")
    with engine.connect() as conn:
        for s in SAMPLES:
            upsert_sample(conn, s)
    print("Done.")


if __name__ == '__main__':
    main()
