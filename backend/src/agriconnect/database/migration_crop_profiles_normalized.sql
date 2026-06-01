-- Migration: normalize crop profile storage for FormationAdvisor-first workflow
-- Date: 2026-04-20
-- Notes:
-- 1) This migration is additive and keeps crop_knowledge for backward compatibility.
-- 2) Run in staging first, then validate counts before production rollout.

BEGIN;

CREATE TABLE IF NOT EXISTS crop_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug VARCHAR(255) UNIQUE NOT NULL,
    crop_name VARCHAR(100) NOT NULL,
    zone_category VARCHAR(50) NOT NULL,
    variety VARCHAR(100),
    scientific_name VARCHAR(255),
    cycle_days INTEGER NOT NULL CHECK (cycle_days > 0),
    depth_cm INTEGER NOT NULL CHECK (depth_cm > 0),
    organic_matter_min_tha NUMERIC(10,2) NOT NULL DEFAULT 0 CHECK (organic_matter_min_tha >= 0),
    water_strategy TEXT,
    inter_row_cm NUMERIC(10,2) NOT NULL CHECK (inter_row_cm > 0),
    inter_plant_cm NUMERIC(10,2) NOT NULL CHECK (inter_plant_cm > 0),
    seeds_pocket INTEGER NOT NULL CHECK (seeds_pocket > 0),
    yield_min_t_ha NUMERIC(10,2) NOT NULL DEFAULT 0 CHECK (yield_min_t_ha >= 0),
    yield_max_t_ha NUMERIC(10,2) NOT NULL DEFAULT 0 CHECK (yield_max_t_ha >= 0),
    key_pests JSONB NOT NULL DEFAULT '[]'::jsonb,
    key_diseases JSONB NOT NULL DEFAULT '[]'::jsonb,
    pre_flight_checks JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT crop_profiles_unique_triplet UNIQUE (crop_name, zone_category, variety)
);

CREATE INDEX IF NOT EXISTS idx_crop_profiles_lookup ON crop_profiles(crop_name, zone_category);
CREATE INDEX IF NOT EXISTS idx_crop_profiles_active ON crop_profiles(is_active);

CREATE TABLE IF NOT EXISTS crop_fertilizer_steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    crop_profile_id UUID NOT NULL REFERENCES crop_profiles(id) ON DELETE CASCADE,
    step_order INTEGER NOT NULL CHECK (step_order > 0),
    stage VARCHAR(120) NOT NULL,
    product_type VARCHAR(120) NOT NULL,
    dose_kg_ha NUMERIC(10,2) NOT NULL CHECK (dose_kg_ha >= 0),
    application_mode VARCHAR(120),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT crop_fertilizer_steps_unique_order UNIQUE (crop_profile_id, step_order)
);

CREATE INDEX IF NOT EXISTS idx_crop_fertilizer_steps_profile ON crop_fertilizer_steps(crop_profile_id);

-- Backfill crop_profiles from legacy JSONB payload in crop_knowledge.
INSERT INTO crop_profiles (
    slug,
    crop_name,
    zone_category,
    variety,
    scientific_name,
    cycle_days,
    depth_cm,
    organic_matter_min_tha,
    water_strategy,
    inter_row_cm,
    inter_plant_cm,
    seeds_pocket,
    yield_min_t_ha,
    yield_max_t_ha,
    key_pests,
    key_diseases,
    pre_flight_checks
)
SELECT
    ck.slug,
    ck.crop_name,
    COALESCE(NULLIF(ck.zone_category, ''), 'Centre') AS zone_category,
    ck.variety,
    COALESCE(ck.technical_sheet->>'scientific_name', '') AS scientific_name,
    COALESCE((ck.technical_sheet->>'cycle_days')::int, 90) AS cycle_days,
    COALESCE((ck.technical_sheet->>'depth_cm')::int, 5) AS depth_cm,
    COALESCE((ck.technical_sheet->>'organic_matter_min_tha')::numeric, 0) AS organic_matter_min_tha,
    ck.technical_sheet->>'water_strategy' AS water_strategy,
    COALESCE((ck.technical_sheet->'sowing_config'->>'inter_row')::numeric, 80) AS inter_row_cm,
    COALESCE((ck.technical_sheet->'sowing_config'->>'inter_plant')::numeric, 40) AS inter_plant_cm,
    COALESCE((ck.technical_sheet->'sowing_config'->>'seeds_pocket')::int, 2) AS seeds_pocket,
    COALESCE((ck.technical_sheet->'yield_potential'->>0)::numeric, 0) AS yield_min_t_ha,
    COALESCE((ck.technical_sheet->'yield_potential'->>1)::numeric, 0) AS yield_max_t_ha,
    COALESCE(ck.technical_sheet->'key_pests', '[]'::jsonb) AS key_pests,
    COALESCE(ck.technical_sheet->'key_diseases', '[]'::jsonb) AS key_diseases,
    COALESCE(ck.technical_sheet->'pre_flight_checks', '[]'::jsonb) AS pre_flight_checks
FROM crop_knowledge ck
ON CONFLICT (slug) DO UPDATE
SET
    crop_name = EXCLUDED.crop_name,
    zone_category = EXCLUDED.zone_category,
    variety = EXCLUDED.variety,
    scientific_name = EXCLUDED.scientific_name,
    cycle_days = EXCLUDED.cycle_days,
    depth_cm = EXCLUDED.depth_cm,
    organic_matter_min_tha = EXCLUDED.organic_matter_min_tha,
    water_strategy = EXCLUDED.water_strategy,
    inter_row_cm = EXCLUDED.inter_row_cm,
    inter_plant_cm = EXCLUDED.inter_plant_cm,
    seeds_pocket = EXCLUDED.seeds_pocket,
    yield_min_t_ha = EXCLUDED.yield_min_t_ha,
    yield_max_t_ha = EXCLUDED.yield_max_t_ha,
    key_pests = EXCLUDED.key_pests,
    key_diseases = EXCLUDED.key_diseases,
    pre_flight_checks = EXCLUDED.pre_flight_checks,
    updated_at = NOW();

-- Backfill fertilizer steps from legacy JSONB array.
INSERT INTO crop_fertilizer_steps (
    crop_profile_id,
    step_order,
    stage,
    product_type,
    dose_kg_ha,
    application_mode
)
SELECT
    cp.id,
    (f.ordinality)::int AS step_order,
    COALESCE(f.item->>'stage', 'Semis') AS stage,
    COALESCE(f.item->>'type', 'NPK') AS product_type,
    COALESCE((f.item->>'dose_kg_ha')::numeric, 0) AS dose_kg_ha,
    COALESCE(f.item->>'mode', 'Epandage') AS application_mode
FROM crop_knowledge ck
JOIN crop_profiles cp ON cp.slug = ck.slug
CROSS JOIN LATERAL jsonb_array_elements(COALESCE(ck.technical_sheet->'fertilizer_plan', '[]'::jsonb)) WITH ORDINALITY AS f(item, ordinality)
ON CONFLICT (crop_profile_id, step_order) DO UPDATE
SET
    stage = EXCLUDED.stage,
    product_type = EXCLUDED.product_type,
    dose_kg_ha = EXCLUDED.dose_kg_ha,
    application_mode = EXCLUDED.application_mode,
    updated_at = NOW();

-- Optional compatibility view for gradual switch-over.
CREATE OR REPLACE VIEW crop_knowledge_compat AS
SELECT
    cp.id,
    cp.slug,
    cp.crop_name,
    cp.variety,
    cp.zone_category,
    jsonb_build_object(
        'name', cp.crop_name,
        'scientific_name', cp.scientific_name,
        'cycle_days', cp.cycle_days,
        'yield_potential', jsonb_build_array(cp.yield_min_t_ha, cp.yield_max_t_ha),
        'sowing_config', jsonb_build_object(
            'inter_row', cp.inter_row_cm,
            'inter_plant', cp.inter_plant_cm,
            'seeds_pocket', cp.seeds_pocket
        ),
        'depth_cm', cp.depth_cm,
        'organic_matter_min_tha', cp.organic_matter_min_tha,
        'water_strategy', cp.water_strategy,
        'fertilizer_plan', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'stage', cfs.stage,
                'type', cfs.product_type,
                'dose_kg_ha', cfs.dose_kg_ha,
                'mode', cfs.application_mode
            ) ORDER BY cfs.step_order)
            FROM crop_fertilizer_steps cfs
            WHERE cfs.crop_profile_id = cp.id
        ), '[]'::jsonb),
        'key_pests', cp.key_pests,
        'key_diseases', cp.key_diseases,
        'pre_flight_checks', cp.pre_flight_checks
    ) AS technical_sheet,
    cp.created_at,
    cp.updated_at
FROM crop_profiles cp
WHERE cp.is_active = TRUE;

COMMIT;
