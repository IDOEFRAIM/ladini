"""Validate the seed insertion by querying normalized tables and computing inputs.
This script runs without importing the full `agriconnect` package to avoid heavy imports.
Requires `DATABASE_URL` env var to be set.
"""
import os
import sys
from sqlalchemy import create_engine, text
from pprint import pprint


def get_db_engine():
    url = os.environ.get('DATABASE_URL') or os.environ.get('AGRICONNECT_DATABASE_URL')
    if not url:
        print('DATABASE_URL not set — cannot validate seed.')
        sys.exit(2)
    return create_engine(url)


def build_canvas_and_calculate(area_ha: float = 2.0):
    eng = get_db_engine()
    with eng.connect() as conn:
        # Fetch profile
        r = conn.execute(text("SELECT id, slug, crop_name, zone_category, inter_row_cm, inter_plant_cm, seeds_pocket FROM crop_profiles WHERE slug = 'mais' LIMIT 1"))
        row = r.first()
        if not row:
            print('No profile found for slug "mais"')
            return None
        profile_id, slug, crop_name, zone, inter_row_cm, inter_plant_cm, seeds_pocket = row

        # Fetch fertilizer plan
        fr = conn.execute(text("SELECT stage, product_type, dose_kg_ha FROM crop_fertilizer_steps WHERE crop_profile_id = :pid ORDER BY step_order"), {'pid': profile_id})
        fert = [dict(stage=row[0], type=row[1], dose_kg_ha=float(row[2])) for row in fr.fetchall()]

    canvas = {
        'meta': {'culture': crop_name, 'variete_recommandee': 'INERA-Hybrid', 'zone': zone},
        'sowing': {'inter_row_cm': float(inter_row_cm), 'inter_plant_cm': float(inter_plant_cm), 'seeds_pocket': int(seeds_pocket)},
        'fertilizer_plan': fert,
    }

    # Calculate inputs per product type
    totals = {}
    for f in fert:
        key = f.get('type')
        dose = f.get('dose_kg_ha', 0) * area_ha
        totals[key] = totals.get(key, 0) + dose

    return canvas, totals


if __name__ == '__main__':
    canvas_totals = build_canvas_and_calculate(2.0)
    if canvas_totals is None:
        sys.exit(1)
    canvas, totals = canvas_totals
    print('\n--- Technical Canvas ---')
    pprint(canvas)
    print('\n--- Totals for 2 ha (kg) ---')
    pprint(totals)
    print('\nExpected: NPK -> 300, Urée -> 200 (if seed applied)')
