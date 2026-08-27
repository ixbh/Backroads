-- Phase 2: Road Intelligence Index schema
-- Everything in this file is derived from OpenStreetMap + open geodata only.
-- Nothing here stores Google Maps Content, so nothing here is subject to
-- Google's caching/indexing restrictions (see architecture doc, §2-3).

CREATE EXTENSION IF NOT EXISTS postgis;

-- Raw road geometry + tags, one row per OSM way we consider "drivable"
CREATE TABLE IF NOT EXISTS roads (
    id              BIGSERIAL PRIMARY KEY,
    osm_id          BIGINT NOT NULL UNIQUE,
    name            TEXT,
    ref             TEXT,
    highway         TEXT NOT NULL,
    surface         TEXT,
    tracktype       TEXT,
    maxspeed        TEXT,
    oneway          BOOLEAN NOT NULL DEFAULT FALSE,
    -- Preserve the direction encoded by OSM. The legacy boolean cannot
    -- distinguish oneway=-1 (travel is opposite the way geometry).
    oneway_direction SMALLINT NOT NULL DEFAULT 0 CHECK (oneway_direction IN (-1, 0, 1)),
    -- Exact OSM node identity prevents a bridge and the road beneath it from
    -- becoming a false intersection merely because their coordinates match.
    node_ids        BIGINT[],
    layer           SMALLINT,
    bridge          BOOLEAN NOT NULL DEFAULT FALSE,
    tunnel          BOOLEAN NOT NULL DEFAULT FALSE,
    lanes           SMALLINT,
    length_m        DOUBLE PRECISION NOT NULL,
    geom            GEOMETRY(LineString, 4326) NOT NULL,
    region          TEXT NOT NULL,
    -- False for road classes that should never be presented as "the fun
    -- road" regardless of how curvy their geometry measures -- residential
    -- streets can have real curves (subdivision loops, cul-de-sacs) but
    -- recommending someone "carve" a street with driveways, kids, and
    -- cyclists on it is a real safety problem, not just a scoring nuance.
    -- Still usable as a connector to physically link a route -- see
    -- architecture doc section 7 -- just never the headline segment.
    scenic_eligible BOOLEAN NOT NULL DEFAULT true,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS roads_geom_idx     ON roads USING GIST (geom);
CREATE INDEX IF NOT EXISTS roads_highway_idx  ON roads (highway);
CREATE INDEX IF NOT EXISTS roads_region_idx   ON roads (region);

-- Traffic signals / stop signs / roundabouts — used for the urban-conflict
-- penalty in Phase 3 (a geometrically curvy street with a stop sign every
-- block should not score like a mountain road).
CREATE TABLE IF NOT EXISTS intersections (
    id      BIGSERIAL PRIMARY KEY,
    osm_id  BIGINT NOT NULL UNIQUE,
    kind    TEXT NOT NULL,          -- 'traffic_signals' | 'stop' | 'mini_roundabout'
    geom    GEOMETRY(Point, 4326) NOT NULL,
    region  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS intersections_geom_idx ON intersections USING GIST (geom);

-- OSM-derived landscape signals used by the modular scenery scorer. Geometry
-- is intentionally generic: viewpoints are points, rivers may be lines, and
-- closed land-use ways become polygons.
CREATE TABLE IF NOT EXISTS scenic_features (
    id          BIGSERIAL PRIMARY KEY,
    osm_id      BIGINT NOT NULL,
    osm_type    CHAR(1) NOT NULL, -- 'n' node | 'w' way
    category    TEXT NOT NULL CHECK (category IN (
                    'water', 'forest', 'park', 'countryside', 'natural', 'viewpoint'
                )),
    name        TEXT,
    geom        GEOMETRY(Geometry, 4326) NOT NULL,
    region      TEXT NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (osm_type, osm_id, category, region)
);

CREATE INDEX IF NOT EXISTS scenic_features_geom_idx ON scenic_features USING GIST (geom);
CREATE INDEX IF NOT EXISTS scenic_features_region_category_idx ON scenic_features (region, category);

-- Computed scores, populated in Phase 3 (curviness) and Phase 4 (scenery).
-- Kept separate from `roads` so re-scoring never touches the source geometry.
CREATE TABLE IF NOT EXISTS road_scores (
    road_id                 BIGINT PRIMARY KEY REFERENCES roads(id) ON DELETE CASCADE,
    curviness_raw           DOUBLE PRECISION,
    curviness_score         SMALLINT,   -- 0-100, percentile-normalized within region
    scenery_raw             DOUBLE PRECISION,
    scenery_score           SMALLINT,
    scenery_signals         JSONB,
    elevation_gain_m        DOUBLE PRECISION,
    elevation_score         SMALLINT,
    urban_conflict_penalty  DOUBLE PRECISION,  -- 0-1 multiplier, see architecture doc §5.4
    composite_score         SMALLINT,
    computed_at             TIMESTAMPTZ
);
