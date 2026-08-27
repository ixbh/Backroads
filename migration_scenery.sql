CREATE TABLE IF NOT EXISTS scenic_features (
    id          BIGSERIAL PRIMARY KEY,
    osm_id      BIGINT NOT NULL,
    osm_type    CHAR(1) NOT NULL,
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

ALTER TABLE road_scores ADD COLUMN IF NOT EXISTS scenery_signals JSONB;
