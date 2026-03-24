-- Seed Data: Sesame and Sorgho (Expansion)
-- Date: 2026-03-22

INSERT INTO crop_knowledge (slug, crop_name, variety, zone_category, technical_sheet) VALUES
(
    'sesame-s42-centre',
    'Sésame',
    'S-42',
    'Centre',
    '{
        "name": "Sésame",
        "scientific_name": "Sesamum indicum",
        "cycle_days": 90,
        "yield_potential": [0.6, 1.2],
        "sowing_config": {"inter_row": 60, "inter_plant": 15, "seeds_pocket": 5},
        "depth_cm": 2,
        "organic_matter_min_tha": 2.5,
        "water_strategy": "Billons cloisonnés (sensible à l''excès d''eau)",
        "fertilizer_plan": [
             {"stage": "Labour/Semis", "type": "NPK 14-23-14", "dose_kg_ha": 100, "mode": "Volée enfouie ou Poquet"}
        ],
        "key_pests": ["Cécidomyie (Asphondylia sesami)", "Chenille enrouleuse (Antigastra)"],
        "key_diseases": ["Cercosporiose", "Fusariose"],
        "pre_flight_checks": ["Sol bien drainé (Vital) ?", "Semence traitée fongicide ?"]
    }'::jsonb
),
(
    'sorgho-kapelga-centre',
    'Sorgho',
    'Kapelga',
    'Centre',
    '{
        "name": "Sorgho",
        "scientific_name": "Sorghum bicolor",
        "cycle_days": 110,
        "yield_potential": [1.5, 3.5],
        "sowing_config": {"inter_row": 80, "inter_plant": 40, "seeds_pocket": 3},
        "depth_cm": 4,
        "organic_matter_min_tha": 5.0,
        "water_strategy": "Zaï et Demi-lunes",
        "fertilizer_plan": [
             {"stage": "Semis", "type": "NPK 14-23-14", "dose_kg_ha": 100, "mode": "Poquet"},
             {"stage": "Montaison", "type": "Urée 46%", "dose_kg_ha": 50, "mode": "Poquet"}
        ],
        "key_pests": ["Striga (Herbe sorcière)", "Mouche des pousses"],
        "key_diseases": ["Charbon", "Mildiou"],
        "pre_flight_checks": ["Terrain infesté de Striga ?", "Rotation culturale ?"]
    }'::jsonb
)
ON CONFLICT (slug) DO UPDATE 
SET technical_sheet = EXCLUDED.technical_sheet, 
    updated_at = NOW();
