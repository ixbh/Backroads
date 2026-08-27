-- Run this once against your existing 'routes' database.
-- Marks residential streets (and living_street, the even-more-pedestrian
-- version) as ineligible to be recommended as "the fun road" -- see
-- architecture doc section 7 and schema.sql for the reasoning. Does NOT
-- delete them; they can still be used as connectors in route generation.

ALTER TABLE roads ADD COLUMN IF NOT EXISTS scenic_eligible BOOLEAN NOT NULL DEFAULT true;

UPDATE roads
SET scenic_eligible = false
WHERE highway IN ('residential', 'living_street');

-- Sanity check: see the split
SELECT highway, scenic_eligible, count(*)
FROM roads
WHERE region = 'minnesota'
GROUP BY highway, scenic_eligible
ORDER BY highway;
