"""Validation runner with DATABASE_URL inlined to avoid shell quoting issues."""
from sqlalchemy import create_engine, text
from pprint import pprint

DATABASE_URL = 'postgresql://ladiniadmin:kingradene@localhost:5433/postgres?sslmode=require'

def main():
    eng = create_engine(DATABASE_URL)
    with eng.connect() as conn:
        row = conn.execute(text("SELECT id, slug, crop_name, zone_category, inter_row_cm, inter_plant_cm, seeds_pocket FROM crop_profiles WHERE slug='mais' LIMIT 1")).first()
        if not row:
            print('No profile found for slug "mais"')
            return 1
        profile_id, slug, crop_name, zone, inter_row_cm, inter_plant_cm, seeds_pocket = row
        fr = conn.execute(text("SELECT stage, product_type, dose_kg_ha FROM crop_fertilizer_steps WHERE crop_profile_id = :pid ORDER BY step_order"), {'pid': profile_id})
        fert = [dict(stage=r[0], type=r[1], dose_kg_ha=float(r[2])) for r in fr.fetchall()]

    canvas = {
        'meta': {'culture': crop_name, 'variete_recommandee': 'INERA-Hybrid', 'zone': zone},
        'sowing': {'inter_row_cm': float(inter_row_cm), 'inter_plant_cm': float(inter_plant_cm), 'seeds_pocket': int(seeds_pocket)},
        'fertilizer_plan': fert,
    }

    totals = {}
    for f in fert:
        key = f.get('type')
        dose = f.get('dose_kg_ha', 0) * 2.0
        totals[key] = totals.get(key, 0) + dose

    print('\n--- Technical Canvas ---')
    pprint(canvas)
    print('\n--- Totals for 2 ha (kg) ---')
    pprint(totals)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
