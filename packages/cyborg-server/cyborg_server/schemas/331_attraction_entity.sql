-- Add attraction as a first-class entity type.
-- Updates CHECK constraint, adds attraction-specific claim types,
-- converts the attraction claim on trips to reference attraction entities.

-- 1. Rebuild memory_entities with 'attraction' in CHECK constraint
CREATE TABLE memory_entities_v7 (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL
        CHECK(entity_type IN ('person','group','location','trip','stay',
                              'event','task','file','thing','decision','connection','attraction')),
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','archived','deprecated')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO memory_entities_v7 SELECT * FROM memory_entities;
DROP TABLE memory_entities;
ALTER TABLE memory_entities_v7 RENAME TO memory_entities;

CREATE INDEX IF NOT EXISTS idx_memory_entities_type
    ON memory_entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_memory_entities_display_name
    ON memory_entities(display_name);

-- 2. Add attraction-specific claim types
INSERT OR IGNORE INTO memory_claim_types (key, applicable_types, description, example) VALUES
    ('attraction_type', '["attraction"]', 'Kind of attraction: temple, museum, beach, market, viewpoint, park, restaurant, bar, activity, tour', 'attraction-tanah-lot → "temple"'),
    ('visit_date', '["attraction"]', 'Date/time when visiting this attraction', 'attraction-tanah-lot → "2026-08-03T16:00"'),
    ('cost', '["attraction"]', 'Cost or ticket price', 'attraction-tanah-lot → "50k IDR"'),
    ('associated_trip', '["attraction"]', 'Trip this attraction belongs to', 'attraction-tanah-lot → trip-bali-2026');

-- 3. Update the existing attraction claim type to reference attraction entities (not locations)
UPDATE memory_claim_types SET
    description = 'An Attraction entity — a thing to see or do on the trip',
    example = 'trip-bali-2026 → attraction-tanah-lot'
WHERE key = 'attraction';

-- 4. Supersede old flat-text attraction claims (rebuild will re-extract as entities)
UPDATE memory_claims
SET status = 'superseded', superseded_by = '["331_attraction_entity"]'
WHERE claim_type_key = 'attraction'
  AND value IS NOT NULL
  AND status = 'active';
