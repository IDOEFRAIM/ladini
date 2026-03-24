-- Migration: Create Knowledge Base for Crop Technical Sheets
-- Date: 2026-03-22
-- Author: AgriConnect Dev

CREATE TABLE IF NOT EXISTS crop_knowledge (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug VARCHAR(255) UNIQUE NOT NULL, -- e.g. 'mais-barka-centre'
    crop_name VARCHAR(100) NOT NULL,
    variety VARCHAR(100),
    zone_category VARCHAR(50), -- 'Nord', 'Centre', 'Sud'
    technical_sheet JSONB NOT NULL, -- Contains density, fertilizer_plan, etc.
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_crop_knowledge_slug ON crop_knowledge(slug);
CREATE INDEX idx_crop_knowledge_lookup ON crop_knowledge(crop_name, zone_category);

-- Seed initial data from the hardcoded dictionary
INSERT INTO crop_knowledge (slug, crop_name, variety, zone_category, technical_sheet) VALUES
(
    'mais-barka-centre',
    'Maïs',
    'Barka',
    'Centre',
    '{
        "name": "Maïs",
        "scientific_name": "Zea mays",
        "cycle_days": 90,
        "yield_potential": [2.5, 5.0],
        "sowing_config": {"inter_row": 75, "inter_plant": 25, "seeds_pocket": 2},
        "depth_cm": 5,
        "organic_matter_min_tha": 5.0,
        "water_strategy": "Zaï et Cordons pierreux",
        "fertilizer_plan": [
            {"stage": "Labour/Semis", "type": "NPK 14-23-14", "dose_kg_ha": 150, "mode": "Bande ou Poquet"},
            {"stage": "30-40 JAS (Sarclage)", "type": "Urée 46%", "dose_kg_ha": 75, "mode": "Poquet couvert"}
        ],
        "key_pests": ["CHENILLE LÉGIONNAIRE D''AUTOMNE (Spodoptera frugiperda)", "Foreurs de tiges (Busseola fusca)"],
        "key_diseases": ["Helminthosporiose", "Striure du maïs"],
        "pre_flight_checks": ["Précédent cultural ?", "Situation topographique (Bas-fond) ?", "Date de semis prévue ?"]
    }'::jsonb
),
(
    'mais-komsaya-nord',
    'Maïs',
    'Komsaya',
    'Nord',
    '{
        "name": "Maïs",
        "scientific_name": "Zea mays",
        "cycle_days": 85,
        "yield_potential": [2.0, 4.0],
        "sowing_config": {"inter_row": 80, "inter_plant": 30, "seeds_pocket": 2},
        "depth_cm": 5,
        "organic_matter_min_tha": 5.0,
        "water_strategy": "Zaï obligatoire",
        "fertilizer_plan": [
             {"stage": "Semis", "type": "NPK 14-23-14", "dose_kg_ha": 100, "mode": "Poquet"},
             {"stage": "30 JAS", "type": "Urée 46%", "dose_kg_ha": 50, "mode": "Poquet"}
        ],
        "key_pests": ["Termites", "Chenille Légionnaire"],
        "key_diseases": ["Charbon"],
        "pre_flight_checks": ["Pluviométrie > 600mm ?"]
    }'::jsonb
),
(
    'niebe-komcalle-centre',
    'Niébé',
    'Komcallé',
    'Centre',
    '{
        "name": "Niébé",
        "scientific_name": "Vigna unguiculata",
        "cycle_days": 70,
        "yield_potential": [0.8, 1.5],
        "sowing_config": {"inter_row": 50, "inter_plant": 20, "seeds_pocket": 2},
        "depth_cm": 3,
        "organic_matter_min_tha": 2.5,
        "water_strategy": "Bandes enherbées",
        "fertilizer_plan": [
             {"stage": "Semis", "type": "NPK 14-23-14", "dose_kg_ha": 100, "mode": "Poquet"},
             {"stage": "Floraison", "type": "Foscapel", "dose_kg_ha": 50, "mode": "Foliaire ou Bande"}
        ],
        "key_pests": ["Maruca vitrata", "Pucerons (Aphis craccivora)"],
        "key_diseases": ["Mosaïque", "Flétrissement bactérien"],
        "pre_flight_checks": ["Traitement insecticide disponible ?"]
    }'::jsonb
)
ON CONFLICT (slug) DO UPDATE 
SET technical_sheet = EXCLUDED.technical_sheet, 
    updated_at = NOW();
