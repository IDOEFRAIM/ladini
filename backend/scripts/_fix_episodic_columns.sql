ALTER TABLE episodic_memories ADD COLUMN IF NOT EXISTS crop VARCHAR(100);
ALTER TABLE episodic_memories ADD COLUMN IF NOT EXISTS zone VARCHAR(100);
CREATE INDEX IF NOT EXISTS idx_episodic_crop ON episodic_memories(crop);
CREATE INDEX IF NOT EXISTS idx_episodic_zone ON episodic_memories(zone);
