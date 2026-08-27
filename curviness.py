"""
Phase 3: Curviness scoring.

Reads road geometry already loaded by Phase 2 and computes a "flow-weighted"
curviness score for each one -- see architecture doc, section 5.1. Writes
curviness_raw and urban_conflict_penalty into road_scores. A separate pass
(normalize_scores) then converts curviness_raw into a 0-100 percentile-based
curviness_score, since "curvy" is relative to what else exists in the region.

Approach, in plain terms:
  1. Re-sample each road's geometry at a fixed step (default 20m) in a
     projected (metric) coordinate system, so distances/angles are accurate
     instead of distorted by lat/lon degrees.
  2. Compute the bearing between each consecutive pair of sampled points.
  3. Compute how much the bearing changes from one sample to the next --
     that's the "turn" at that point.
  4. Classify each turn by how sharp it is per sampling step (a proxy for
     radius) into sweeper / moderate / hairpin, and weight it accordingly.
     Sweepers get the highest weight (most enjoyable at speed); hairpins get
     a weight that *decreases* with how frequently they occur, since a road
     that's nothing but back-to-back hairpins is stressful, not fun.
  5. Sum the weighted turning into a per-km raw curviness figure.
  6. Separately, penalize roads with lots of nearby intersections
     (traffic_signals/stop/mini_roundabout) per km -- a technically curvy
     street with a stop sign every block isn't a "carving" road.

This is intentionally a heuristic, not an exact physical model -- there is
no ground truth for "how fun is this road," so the goal is a defensible,
inspectable approximation, not false precision.

Usage:
    python curviness.py --region minnesota
"""

from __future__ import annotations

import argparse
import functools
import logging
import math
import os
import sys

import psycopg2
import psycopg2.extras
from pyproj import Transformer
from shapely import wkb as shapely_wkb
from shapely.geometry import LineString

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("curviness")

# --- Tunable constants -------------------------------------------------
SAMPLE_STEP_M = 20.0          # re-sampling interval along the road, in meters
MIN_ROAD_LENGTH_M = 40.0      # roads shorter than this get a flat low score

# Per-sample turn angle (degrees) thresholds -> bucket
SWEEPER_MAX_DEG = 25.0        # 0-25 deg per 20m step: flowing sweeper
MODERATE_MAX_DEG = 60.0       # 25-60 deg per step: moderate curve
# > 60 deg per 20m step: treated as a hairpin

# Flow-weighting per bucket (higher = more "fun per degree of turning")
SWEEPER_WEIGHT = 1.3
MODERATE_WEIGHT = 1.0
HAIRPIN_BASE_WEIGHT = 0.9

# Hairpins in isolation are great; a road that's ALL hairpins is stressful.
# This knocks the hairpin weight down as hairpin frequency (per km) rises.
HAIRPIN_DENSITY_SOFTENING = 0.06   # higher = softens faster with density

# Urban-conflict penalty: intersections within this buffer count as "on" the road
INTERSECTION_BUFFER_M = 15.0
# Decay rate for the penalty multiplier as intersection density (per km) rises.
# multiplier = exp(-density_per_km / URBAN_DECAY) -- at density == URBAN_DECAY,
# the multiplier is ~0.37; roughly one signal every ~(1000/URBAN_DECAY) meters.
URBAN_DECAY = 4.0


@functools.lru_cache(maxsize=64)
def _cached_transformer(epsg: int) -> Transformer:
    """Building a pyproj Transformer is not cheap (tens of ms) -- doing it
    fresh for every road (hundreds of thousands of times) was a real,
    measured bottleneck. Almost every road in a single region shares the
    same UTM zone, so cache one Transformer per zone actually encountered."""
    return Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)


def _get_utm_transformer(lon: float, lat: float) -> Transformer:
    """Pick an appropriate UTM zone for accurate metric distances/angles,
    based on a representative point (e.g. the road's centroid). Keeps this
    script correct for any US region, not just Minnesota."""
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return _cached_transformer(epsg)


def _bearing_deg(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    return math.degrees(math.atan2(dx, dy)) % 360.0


def _angle_diff_deg(a: float, b: float) -> float:
    """Smallest signed difference b-a, wrapped to [-180, 180]."""
    d = (b - a + 180.0) % 360.0 - 180.0
    return d


def score_curviness(line_wgs84: LineString, length_m: float) -> tuple[float, dict]:
    """Returns (curviness_raw_per_km, debug_info)."""
    if length_m < MIN_ROAD_LENGTH_M or len(line_wgs84.coords) < 2:
        return 0.0, {"reason": "too_short"}

    lon0, lat0 = line_wgs84.coords[len(line_wgs84.coords) // 2]
    transformer = _get_utm_transformer(lon0, lat0)
    projected_coords = [transformer.transform(x, y) for x, y in line_wgs84.coords]
    line_m = LineString(projected_coords)

    n_samples = max(2, int(line_m.length // SAMPLE_STEP_M) + 1)
    sample_points = [
        line_m.interpolate(min(i * SAMPLE_STEP_M, line_m.length)).coords[0]
        for i in range(n_samples)
    ]
    if sample_points[-1] != line_m.coords[-1]:
        sample_points.append(line_m.coords[-1])

    if len(sample_points) < 3:
        return 0.0, {"reason": "too_few_samples"}

    bearings = [
        _bearing_deg(sample_points[i], sample_points[i + 1])
        for i in range(len(sample_points) - 1)
    ]
    turns = [
        abs(_angle_diff_deg(bearings[i], bearings[i + 1]))
        for i in range(len(bearings) - 1)
    ]

    sweeper_count = sum(1 for t in turns if t <= SWEEPER_MAX_DEG)
    moderate_count = sum(1 for t in turns if SWEEPER_MAX_DEG < t <= MODERATE_MAX_DEG)
    hairpin_count = sum(1 for t in turns if t > MODERATE_MAX_DEG)

    length_km = length_m / 1000.0
    hairpin_density_per_km = hairpin_count / length_km if length_km > 0 else 0.0
    hairpin_weight = HAIRPIN_BASE_WEIGHT * math.exp(
        -HAIRPIN_DENSITY_SOFTENING * hairpin_density_per_km
    )

    weighted_turn_sum = 0.0
    for t in turns:
        if t <= SWEEPER_MAX_DEG:
            weighted_turn_sum += t * SWEEPER_WEIGHT
        elif t <= MODERATE_MAX_DEG:
            weighted_turn_sum += t * MODERATE_WEIGHT
        else:
            weighted_turn_sum += t * hairpin_weight

    curviness_raw = weighted_turn_sum / length_km if length_km > 0 else 0.0

    debug = {
        "n_samples": len(sample_points),
        "sweeper_count": sweeper_count,
        "moderate_count": moderate_count,
        "hairpin_count": hairpin_count,
        "hairpin_weight_used": round(hairpin_weight, 3),
    }
    return curviness_raw, debug


def _meters_to_degrees(meters: float, latitude_deg: float) -> float:
    """Approximate meters -> degrees conversion for a small buffer distance.
    Uses the longitude-direction conversion (which shrinks with latitude),
    since that's the SMALLER meters-per-degree value at mid-to-high
    latitudes -- using it means the buffer errs slightly generous rather
    than under-covering intersections along east-west roads. This is a
    heuristic "is a stoplight basically on this road" signal, not a
    survey-grade measurement, so a small approximation is an acceptable
    trade for a query that can actually use a plain-geometry index."""
    meters_per_deg_lat = 111_320.0
    meters_per_deg_lon = 111_320.0 * math.cos(math.radians(latitude_deg))
    return meters / min(meters_per_deg_lat, meters_per_deg_lon)


def fetch_urban_conflict_penalties(conn, region: str) -> dict[int, float]:
    """One bulk query for the whole region instead of one query per road.
    Regardless of how large the per-row cost turns out to be on any given
    machine/dataset, 300K individual round trips to Postgres is strictly
    worse than one -- this removes that variable entirely.

    Deliberately uses ST_DWithin on the plain `geometry` column (degrees),
    against the index Phase 2 already created (`intersections_geom_idx`),
    rather than casting to `::geography`. Both are "correct," but in testing
    the geography-cast version needed a new expression index and still ran
    slower on this data -- the plain-geometry version uses Phase 2's
    existing index as-is and was the fastest option actually measured.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT avg(ST_Y(ST_PointN(geom, 1))) FROM roads WHERE region = %s",
            (region,),
        )
        (avg_lat,) = cur.fetchone()
        if avg_lat is None:
            return {}

        buffer_deg = _meters_to_degrees(INTERSECTION_BUFFER_M, avg_lat)
        logger.info(
            "Using %.6f degrees as the ~%.0fm urban-conflict buffer (at latitude %.2f)",
            buffer_deg, INTERSECTION_BUFFER_M, avg_lat,
        )

        cur.execute(
            """
            SELECT r.id, count(i.id)
            FROM roads r
            LEFT JOIN intersections i
              ON ST_DWithin(i.geom, r.geom, %s)
             AND i.region = r.region
            WHERE r.region = %s
            GROUP BY r.id
            """,
            (buffer_deg, region),
        )
        penalties: dict[int, float] = {}
        for road_id, conflict_count in cur.fetchall():
            # length_km is looked up again at score time; here we just need
            # the raw count, so store it and compute the actual multiplier
            # where we have length_m in the main loop.
            penalties[road_id] = conflict_count
    return penalties


def urban_conflict_multiplier(conflict_count: int, length_m: float) -> float:
    length_km = max(length_m / 1000.0, 0.001)
    density_per_km = conflict_count / length_km
    return math.exp(-density_per_km / URBAN_DECAY)


UPSERT_SQL = """
    INSERT INTO road_scores (road_id, curviness_raw, urban_conflict_penalty, computed_at)
    VALUES %s
    ON CONFLICT (road_id) DO UPDATE SET
        curviness_raw = EXCLUDED.curviness_raw,
        urban_conflict_penalty = EXCLUDED.urban_conflict_penalty,
        computed_at = EXCLUDED.computed_at
"""
UPSERT_TEMPLATE = "(%s, %s, %s, now())"


def run(region: str, database_url: str, read_batch: int = 500, write_batch: int = 1000) -> None:
    conn = psycopg2.connect(database_url)
    conn.autocommit = False
    try:
        logger.info("Precomputing urban-conflict counts for region=%s ...", region)
        conflict_counts = fetch_urban_conflict_penalties(conn, region)
        logger.info("Got conflict counts for %d roads.", len(conflict_counts))

        with conn.cursor(name="road_cursor") as read_cur:
            read_cur.itersize = read_batch
            read_cur.execute(
                "SELECT id, ST_AsBinary(geom) AS geom_wkb, length_m FROM roads WHERE region = %s",
                (region,),
            )

            with conn.cursor() as write_cur:
                total = 0
                pending: list[tuple] = []

                for road_id, geom_wkb, length_m in read_cur:
                    line = shapely_wkb.loads(bytes(geom_wkb))
                    curviness_raw, _debug = score_curviness(line, length_m)
                    conflict_count = conflict_counts.get(road_id, 0)
                    urban_penalty = urban_conflict_multiplier(conflict_count, length_m)

                    pending.append((road_id, curviness_raw, urban_penalty))
                    total += 1

                    if len(pending) >= write_batch:
                        psycopg2.extras.execute_values(
                            write_cur, UPSERT_SQL, pending, template=UPSERT_TEMPLATE
                        )
                        pending.clear()
                        # Deliberately no conn.commit() here -- the read_cur
                        # named cursor is still open mid-fetch, and committing
                        # would invalidate it (see Phase 3 v1 bug). One commit
                        # happens after the whole loop finishes instead.
                        logger.info("Scored %d roads so far...", total)

                if pending:
                    psycopg2.extras.execute_values(
                        write_cur, UPSERT_SQL, pending, template=UPSERT_TEMPLATE
                    )

        conn.commit()
        logger.info("Done scoring %d roads for region=%s.", total, region)
        normalize_scores(conn, region)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def normalize_scores(conn, region: str) -> None:
    """Converts curviness_raw into a 0-100 percentile-based curviness_score
    within this region -- 'curvy' is relative to what else is nearby, not an
    absolute physical unit (see architecture doc, section 5.4)."""
    logger.info("Normalizing curviness scores for region=%s ...", region)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE road_scores rs
            SET curviness_score = sub.pct
            FROM (
                SELECT rs2.road_id,
                       round(percent_rank() OVER (ORDER BY rs2.curviness_raw) * 100)::smallint AS pct
                FROM road_scores rs2
                JOIN roads r ON r.id = rs2.road_id
                WHERE r.region = %s
            ) sub
            WHERE rs.road_id = sub.road_id
            """,
            (region,),
        )
    conn.commit()
    logger.info("Normalization done.")


def main() -> None:
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
