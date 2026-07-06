-- Seed: INERA Sahelian standard profiles (Maïs)
BEGIN;

-- Upsert crop profile for Maïs (slug: mais)
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
    pre_flight_checks,
    is_active
)
VALUES (
    'mais',
    'Maïs',
    'Centre',
    'INERA-Hybrid',
    '',
    90,
    5,
    5.00,
    '',
    75,
    25,
    2,
    0,
    0,
    '[]'::jsonb,
    '[]'::jsonb,
    '[]'::jsonb,
    TRUE
)
ON CONFLICT (slug) DO UPDATE SET
    crop_name = EXCLUDED.crop_name,
    zone_category = EXCLUDED.zone_category,
    variety = EXCLUDED.variety,
    cycle_days = EXCLUDED.cycle_days,
    depth_cm = EXCLUDED.depth_cm,
    organic_matter_min_tha = EXCLUDED.organic_matter_min_tha,
    inter_row_cm = EXCLUDED.inter_row_cm,
    inter_plant_cm = EXCLUDED.inter_plant_cm,
    seeds_pocket = EXCLUDED.seeds_pocket,
    updated_at = NOW();

-- Insert fertilizer steps for Maïs (ordered)
-- We resolve crop_profile_id by slug
WITH cp AS (
    SELECT id FROM crop_profiles WHERE slug = 'mais' LIMIT 1
)
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
    s.step_order,
    s.stage,
    s.product_type,
    s.dose_kg_ha,
    s.application_mode
FROM cp, (
    VALUES
        (1, 'Préparation', 'Fumure organique', 5.00, 'Epandage'),
        (2, 'Semis', 'NPK', 150.00, 'Epandage'),
        (3, '30 jours', 'Urée', 50.00, 'Epandage'),
        (4, '45 jours', 'Urée', 50.00, 'Epandage')
) AS s(step_order, stage, product_type, dose_kg_ha, application_mode)
ON CONFLICT (crop_profile_id, step_order) DO UPDATE SET
    stage = EXCLUDED.stage,
    product_type = EXCLUDED.product_type,
    dose_kg_ha = EXCLUDED.dose_kg_ha,
    application_mode = EXCLUDED.application_mode,
    updated_at = NOW();

COMMIT;
