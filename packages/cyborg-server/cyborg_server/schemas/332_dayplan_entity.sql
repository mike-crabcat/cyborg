-- Add dayplan as a first-class entity type.
-- Updates CHECK constraint, adds dayplan-specific claim types,
-- adds dayplan claim on trips.

-- 1. Rebuild memory_entities with 'dayplan' in CHECK constraint
CREATE TABLE memory_entities_v8 (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL
        CHECK(entity_type IN ('person','group','location','trip','stay',
                              'event','task','file','thing','decision','connection','attraction','dayplan')),
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','archived','deprecated')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO memory_entities_v8 SELECT * FROM memory_entities;
DROP TABLE memory_entities;
ALTER TABLE memory_entities_v8 RENAME TO memory_entities;

CREATE INDEX IF NOT EXISTS idx_memory_entities_type
    ON memory_entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_memory_entities_display_name
    ON memory_entities(display_name);

-- 2. Add dayplan-specific claim types
INSERT OR IGNORE INTO memory_claim_types (key, applicable_types, description, example) VALUES
    ('date', '["dayplan"]', 'Date this dayplan covers', 'dayplan-bali-aug3 → "2026-08-03"'),
    ('notes', '["dayplan"]', 'Ideas, plans, or notes for the day', 'dayplan-bali-aug3 → "Morning at beach, afternoon temple visit"');

-- 3. Add dayplan claim type on trips
INSERT OR IGNORE INTO memory_claim_types (key, applicable_types, description, example) VALUES
    ('dayplan', '["trip"]', 'A Dayplan entity — planned itinerary for a specific day', 'trip-bali-2026 → dayplan-bali-aug3');

-- 4. Update associated_trip to include dayplan
UPDATE memory_claim_types
SET applicable_types = REPLACE(applicable_types, ']', ',"dayplan"]')
WHERE key = 'associated_trip'
  AND applicable_types NOT LIKE '%"dayplan"%';
