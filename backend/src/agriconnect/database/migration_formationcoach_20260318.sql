-- Migration: Add tables for FormationCoach expert assistant
-- Generated: 2026-03-18

-- Ensure uuid/ossp and pgcrypto available
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pgcrypto;

SET search_path TO public;

-- 1) Crop technical sheets (fiches techniques)
CREATE TABLE IF NOT EXISTS crop_tech_sheets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  crop_name TEXT NOT NULL UNIQUE,
  lifecycle JSONB, -- stages and durations
  needs JSONB, -- nutrient/water/soil needs
  seasonality JSONB, -- months/seasons mapping
  common_errors JSONB, -- list of frequent mistakes
  recommendations JSONB, -- keyed recommendations
  sources JSONB,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_crop_tech_name ON crop_tech_sheets(LOWER(crop_name));

-- 2) Diagnostics history
CREATE TABLE IF NOT EXISTS diagnostics (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID,
  crop_name TEXT,
  zone_id TEXT,
  query TEXT,
  diagnosis TEXT,
  evidence JSONB,
  severity TEXT,
  rules_used JSONB,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_diagnostics_user ON diagnostics(user_id);
CREATE INDEX IF NOT EXISTS idx_diagnostics_crop ON diagnostics(crop_name);

-- 3) Action plans linked to diagnostics
CREATE TABLE IF NOT EXISTS action_plans (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  diagnostic_id UUID REFERENCES diagnostics(id) ON DELETE CASCADE,
  actions_immediate JSONB,
  actions_7_days JSONB,
  risks JSONB,
  sources JSONB,
  created_by TEXT,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_action_plans_diag ON action_plans(diagnostic_id);

-- 4) User memories (lightweight time-scoped facts)
CREATE TABLE IF NOT EXISTS user_memories (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL,
  memory_type TEXT NOT NULL,
  payload JSONB NOT NULL,
  expires_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_user_memories_user ON user_memories(user_id);

-- 5) Feedback for evaluations
CREATE TABLE IF NOT EXISTS formation_feedback (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  diagnostic_id UUID REFERENCES diagnostics(id) ON DELETE SET NULL,
  user_id UUID,
  rating INTEGER, -- 1..5
  comment TEXT,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_feedback_diag ON formation_feedback(diagnostic_id);

-- Migration complete
