"""Compute explainable OSM-derived scenery scores for imported roads.

Scores are absolute 0-100 signal sums, not regional percentiles. A road earns
points only for named nearby features, stored in scenery_signals for the API.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg2
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("scenery")

SIGNAL_WEIGHTS = {
    "water": 30,
    "forest": 25,
    "park": 15,
    "countryside": 12,
    "natural": 18,
    "viewpoint": 15,
    # Kept separate from the foliage/landscape score. These signals power the
    # optional sightseeing mode without redefining what the scenery slider means.
    "attraction": 0,
    "monument": 0,
}


def score_signals(signals: set[str]) -> int:
    """Pure scoring contract used by tests and mirrored by the SQL update."""
    return min(100, sum(SIGNAL_WEIGHTS.get(signal, 0) for signal in signals))


SCORING_SQL = """
WITH signals AS (
    SELECT r.id AS road_id,
           bool_or(f.category = 'water'      AND ST_DWithin(r.geom, f.geom, 0.00625)) AS water,
           bool_or(f.category = 'forest'     AND ST_DWithin(r.geom, f.geom, 0.00500)) AS forest,
           bool_or(f.category = 'park'       AND ST_DWithin(r.geom, f.geom, 0.00625)) AS park,
           bool_or(f.category = 'countryside' AND ST_DWithin(r.geom, f.geom, 0.00313)) AS countryside,
           bool_or(f.category = 'natural'    AND ST_DWithin(r.geom, f.geom, 0.00625)) AS natural_land,
           bool_or(f.category = 'viewpoint'  AND ST_DWithin(r.geom, f.geom, 0.01875)) AS viewpoint,
           bool_or(f.category = 'attraction' AND ST_DWithin(r.geom, f.geom, 0.01875)) AS attraction,
           bool_or(f.category = 'monument'   AND ST_DWithin(r.geom, f.geom, 0.01875)) AS monument
    FROM roads r
    LEFT JOIN scenic_features f
      ON f.region = r.region
     AND f.geom && ST_Expand(r.geom, 0.01875)
    WHERE r.region = %s
    GROUP BY r.id
), scored AS (
    SELECT road_id, water, forest, park, countryside, natural_land, viewpoint,
           attraction, monument,
           LEAST(100,
               CASE WHEN water       THEN 30 ELSE 0 END +
               CASE WHEN forest      THEN 25 ELSE 0 END +
               CASE WHEN park        THEN 15 ELSE 0 END +
               CASE WHEN countryside THEN 12 ELSE 0 END +
               CASE WHEN natural_land THEN 18 ELSE 0 END +
               CASE WHEN viewpoint   THEN 15 ELSE 0 END
           )::smallint AS score
    FROM signals
)
INSERT INTO road_scores (road_id, scenery_raw, scenery_score, scenery_signals, computed_at)
SELECT road_id, score, score,
       jsonb_strip_nulls(jsonb_build_object(
           'water',       CASE WHEN water       THEN true END,
           'forest',      CASE WHEN forest      THEN true END,
           'park',        CASE WHEN park        THEN true END,
           'countryside', CASE WHEN countryside THEN true END,
           'natural',     CASE WHEN natural_land THEN true END,
           'viewpoint',   CASE WHEN viewpoint   THEN true END,
           'attraction',  CASE WHEN attraction  THEN true END,
           'monument',    CASE WHEN monument    THEN true END
       )), now()
FROM scored
ON CONFLICT (road_id) DO UPDATE SET
    scenery_raw = EXCLUDED.scenery_raw,
    scenery_score = EXCLUDED.scenery_score,
    scenery_signals = EXCLUDED.scenery_signals,
    computed_at = EXCLUDED.computed_at
"""


def run(region: str, database_url: str):
    conn = psycopg2.connect(database_url)
    try:
        with conn:
            with conn.cursor() as cursor:
                logger.info("Scoring OSM scenery signals for region=%s...", region)
                cursor.execute(SCORING_SQL, (region,))
                cursor.execute(
                    "SELECT count(*), round(avg(scenery_score), 1), max(scenery_score) "
                    "FROM road_scores rs JOIN roads r ON r.id=rs.road_id WHERE r.region=%s",
                    (region,),
                )
                count, average, maximum = cursor.fetchone()
        logger.info("Scored %d roads; average=%s, maximum=%s.", count, average, maximum)
    finally:
        conn.close()


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", required=True)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url:
        logger.error("No database URL provided. Set DATABASE_URL or pass --database-url.")
        sys.exit(1)
    run(args.region, args.database_url)


if __name__ == "__main__":
    main()
