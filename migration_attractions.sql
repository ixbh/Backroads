-- Add named sightseeing signals without folding them into the foliage-based
-- scenery score. Re-run load_scenic_features.py and scenery.py afterwards.
ALTER TABLE scenic_features
    DROP CONSTRAINT IF EXISTS scenic_features_category_check;

ALTER TABLE scenic_features
    ADD CONSTRAINT scenic_features_category_check CHECK (category IN (
        'water', 'forest', 'park', 'countryside', 'natural', 'viewpoint',
        'attraction', 'monument'
    ));
