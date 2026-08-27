-- Preserve enough OSM topology to avoid false intersections at bridges and
-- correctly represent reverse one-way roads. Safe to run more than once.
ALTER TABLE roads ADD COLUMN IF NOT EXISTS oneway_direction SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE roads ADD COLUMN IF NOT EXISTS node_ids BIGINT[];
ALTER TABLE roads ADD COLUMN IF NOT EXISTS layer SMALLINT;
ALTER TABLE roads ADD COLUMN IF NOT EXISTS bridge BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE roads ADD COLUMN IF NOT EXISTS tunnel BOOLEAN NOT NULL DEFAULT FALSE;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'roads_oneway_direction_check'
    ) THEN
        ALTER TABLE roads ADD CONSTRAINT roads_oneway_direction_check
            CHECK (oneway_direction IN (-1, 0, 1));
    END IF;
END $$;

-- Preserve old behavior until each row is refreshed from its source PBF.
UPDATE roads SET oneway_direction = 1 WHERE oneway AND oneway_direction = 0;
