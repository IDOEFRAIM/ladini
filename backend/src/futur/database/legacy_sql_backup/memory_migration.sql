-- ============================================
-- Migration: Architecture Mémoire 3 Niveaux
-- ============================================
-- Niveau 1: Profil Agricole Structuré (user_farm_profiles)
-- Niveau 2: Mémoire Épisodique (episodic_memories)
-- ============================================
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================
-- NIVEAU 1 : PROFIL FERME STRUCTURÉ (Long-Terme)
-- ============================================
-- Stocke la "Fiche Ferme" JSONB de chaque agriculteur.
-- Coût lecture : 0 tokens LLM (SQL pur).
-- Injection dans le prompt : ~80 tokens JSON au lieu de ~2000 d'historique.

CREATE TABLE IF NOT EXISTS user_farm_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id VARCHAR(255) UNIQUE NOT NULL,
    
    -- Le cœur : profil JSONB (parcelles, bétail, contraintes...)
    profile_data JSONB NOT NULL DEFAULT '{
        "location": {},
        "plots": [],
        "livestock": [],
        "equipment": [],
        "preferences": {"language": "fr", "level": "debutant"},
        "constraints": {}
    }'::jsonb,
    
    -- Versioning pour migrations futures du schéma JSON
    version VARCHAR(10) DEFAULT '1',
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Index sur user_id (lookup rapide)
CREATE INDEX IF NOT EXISTS idx_farm_profile_user ON user_farm_profiles(user_id);

-- Index GIN sur le JSONB (recherches dans les parcelles, cultures...)
CREATE INDEX IF NOT EXISTS idx_farm_profile_data ON user_farm_profiles USING GIN (profile_data);

-- Index sur les cultures actives (pour requêtes du type "tous les producteurs de maïs")
CREATE INDEX IF NOT EXISTS idx_farm_profile_crops ON user_farm_profiles 
    USING GIN ((profile_data -> 'plots'));


-- ============================================
-- NIVEAU 2 : MÉMOIRE ÉPISODIQUE (Moyen-Terme)
-- ============================================
-- Stocke les résumés d'interactions significatives.
-- Un résumé = ~30 tokens. 100 interactions = ~3000 tokens stockés.
-- Rappel : on ne charge que les 3-5 plus pertinentes (~150 tokens).

CREATE TABLE IF NOT EXISTS episodic_memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id VARCHAR(255) NOT NULL,
    
    -- Résumé court optimisé pour injection LLM (~30 tokens)
    summary TEXT NOT NULL,
    
    -- Catégorie pour filtrage SQL rapide (évite vector search)
    category VARCHAR(50) NOT NULL,  -- diagnosis | market | weather | formation | soil | general
    
    -- Entités clés pour filtrage contextuel
    crop VARCHAR(100),
    zone VARCHAR(100),
    severity VARCHAR(20),  -- INFO | WARNING | HAUT | CRITIQUE
    
    -- Pertinence (décroît avec le temps via decay job)
    relevance_score FLOAT DEFAULT 1.0,
    access_count INTEGER DEFAULT 0,
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Index principal : lookup par utilisateur + tri pertinence
CREATE INDEX IF NOT EXISTS idx_episodic_user_relevance 
    ON episodic_memories(user_id, relevance_score DESC, created_at DESC);

-- Index pour filtrage par culture (ex: "rappelle les épisodes sur le maïs")
CREATE INDEX IF NOT EXISTS idx_episodic_crop ON episodic_memories(crop);

-- Index pour filtrage par zone
CREATE INDEX IF NOT EXISTS idx_episodic_zone ON episodic_memories(zone);

-- Index pour filtrage par catégorie
CREATE INDEX IF NOT EXISTS idx_episodic_category ON episodic_memories(category);

-- Nettoyage automatique des épisodes très anciens et non pertinents
-- (à exécuter périodiquement via un job Celery)
-- DELETE FROM episodic_memories 
-- WHERE relevance_score < 0.05 AND created_at < NOW() - INTERVAL '12 months';


-- ============================================
-- TRIGGER : Auto-update updated_at sur user_farm_profiles
-- ============================================

CREATE TRIGGER update_farm_profiles_updated_at 
    BEFORE UPDATE ON user_farm_profiles 
    FOR EACH ROW 
    EXECUTE FUNCTION update_updated_at_column();


-- ============================================
-- GRANTS
-- ============================================

GRANT ALL PRIVILEGES ON user_farm_profiles TO agriconnect;
GRANT ALL PRIVILEGES ON episodic_memories TO agriconnect;


-- ============================================
-- CONFIRMATION
-- ============================================

DO $$
BEGIN
    RAISE NOTICE '✅ Migration Mémoire 3 Niveaux terminée!';
    RAISE NOTICE '📋 Table user_farm_profiles: Profil structuré JSONB (Niveau 1)';
    RAISE NOTICE '📝 Table episodic_memories: Résumés épisodiques (Niveau 2)';
    RAISE NOTICE '🧠 Le Niveau 3 (ContextOptimizer) est en code Python';
END $$;
