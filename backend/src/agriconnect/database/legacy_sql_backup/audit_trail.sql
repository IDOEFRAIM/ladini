-- Table pour garantir la traçabilité des décisions des agents (Preuve de Protocole)
-- Utile pour le Credit Scoring et l'audit bancaire.

CREATE TABLE IF NOT EXISTS agent_audit_trails (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    agent_name VARCHAR(50) NOT NULL,        -- ex: "FormationCoach", "MarketplaceAgent"
    action_type VARCHAR(50) NOT NULL,       -- ex: "ADVICE_GIVEN", "TRANSACTION_MATCHED", "ALERT_SENT"
    
    user_id VARCHAR(100),                   -- ID de l'utilisateur concerné
    session_id VARCHAR(100),                -- ID de la conversation/session
    
    protocol_used VARCHAR(20),              -- "MCP", "A2A", "INTERNAL"
    resource_accessed VARCHAR(100),         -- ex: "mcp://neondb/products", "mcp://rag/docs"
    
    decision_payload JSONB,                 -- Le contenu de la décision/conseil
    confidence_score FLOAT,                 -- Score de confiance de l'IA (si applicable)
    
    hash_signature VARCHAR(255)             -- (Optionnel) Hash pour sceller l'enregistrement
);

CREATE INDEX IF NOT EXISTS idx_audit_user ON agent_audit_trails(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_agent ON agent_audit_trails(agent_name);
