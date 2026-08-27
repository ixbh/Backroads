"""
Phase 2: Candidate road collection.

Parses a regional OpenStreetMap extract (.osm.pbf) and loads drivable roads
and traffic-control nodes into PostGIS. This is the permanent, free-to-index
"Road Intelligence" data layer described in the architecture doc (§2-4) --
none of it is Google Maps Content, so none of it is subject to Google's
caching/indexing restrictions.

Usage:
    python load_osm.py --pbf minnesota-latest.osm.pbf --region minnesota

Requires:
    pip install osmium psycopg2-binary
    DATABASE_URL env var, e.g. postgresql://user:pass@localhost:5432/routes
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import osmium
import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("load_osm")

# highway=* values we consider drivable for a scenic-route candidate graph.
# Deliberately excludes footway/cycleway/steps/pedestrian/bridleway/path.
DRIVABLE_HIGHWAY = {
    "motorway", "motorway_link",
    "trunk", "trunk_link",
    "primary", "primary_link",
    "secondary", "secondary_link",
    "tertiary", "tertiary_link",
    "unclassified",
    "residential",
    "living_street",
    "track",  # kept only if not a poor-quality track -- see tracktype filter below
}

# tracktype=grade4/grade5 means "barely passable" -- exclude those even
# though highway=track is otherwise in the allow-list above.
EXCLUDED_TRACKTYPES = {"grade4", "grade5"}

INTERSECTION_KINDS = {"traffic_signals", "stop", "mini_roundabout"}


def _safe_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        # lanes is sometimes "2;3" for varying lane counts -- take the first
        return int(value.split(";")[0].strip())
    except (TypeError, ValueError):
        return None


def _oneway_direction(tags) -> int:
    """Return 1 for geometry direction, -1 for reverse, and 0 for both."""
    value = (tags.get("oneway") or "").strip().lower()
    if value == "-1":
        return -1
    if value in ("yes", "1", "true"):
        return 1
    if value in ("no", "0", "false"):
        return 0
    if tags.get("junction") in ("roundabout", "circular") or tags.get("highway") == "motorway":
        return 1
    return 0


class RoadHandler(osmium.SimpleHandler):
    """Collects drivable ways and traffic-control nodes in memory.

    For a single state/regional extract this comfortably fits in RAM.
    If you later ingest something planet-scale, switch this to a streaming
    insert (flush every N ways) instead of collecting into lists first.
    """

    def __init__(self, wkbfab: osmium.geom.WKBFactory):
        super().__init__()
        self.wkbfab = wkbfab
        self.roads: list[dict] = []
        self.intersections: list[dict] = []
        self._skipped_no_location = 0
        self._skipped_too_short = 0

    def way(self, w: osmium.osm.Way) -> None:
        tags = w.tags
        highway = tags.get("highway")
        if highway not in DRIVABLE_HIGHWAY:
            return
        if highway == "track" and tags.get("tracktype") in EXCLUDED_TRACKTYPES:
            return
        if tags.get("access") in ("private", "no"):
            return

        try:
            wkb_hex = self.wkbfab.create_linestring(w)
        except osmium.InvalidLocationError:
            self._skipped_no_location += 1
            return
        except RuntimeError:
            # e.g. a way left with fewer than 2 usable points after de-duping
            self._skipped_too_short += 1
            return

        self.roads.append(
            {
                "osm_id": w.id,
                "name": tags.get("name"),
                "ref": tags.get("ref"),
                "highway": highway,
                "surface": tags.get("surface"),
                "tracktype": tags.get("tracktype"),
                "maxspeed": tags.get("maxspeed"),
                "oneway_direction": _oneway_direction(tags),
                "node_ids": [node.ref for node in w.nodes],
                "layer": _safe_int(tags.get("layer")),
                "bridge": tags.get("bridge") not in (None, "no", "false", "0"),
                "tunnel": tags.get("tunnel") not in (None, "no", "false", "0"),
                "lanes": _safe_int(tags.get("lanes")),
                "wkb_hex": wkb_hex,
            }
        )

    def node(self, n: osmium.osm.Node) -> None:
        highway = n.tags.get("highway")
        if highway in INTERSECTION_KINDS:
            self.intersections.append(
                {
                    "osm_id": n.id,
                    "kind": highway,
                    "lon": n.location.lon,
                    "lat": n.location.lat,
                }
            )


def _insert_roads(cur, roads: list[dict], region: str, page_size: int) -> None:
    if not roads:
        logger.warning("No roads to insert.")
        return

    sql = """
        INSERT INTO roads
            (osm_id, name, ref, highway, surface, tracktype, maxspeed,
             oneway, oneway_direction, node_ids, layer, bridge, tunnel,
             lanes, length_m, geom, region, scenic_eligible)
        VALUES %s
        ON CONFLICT (osm_id) DO UPDATE SET
            name      = EXCLUDED.name,
            ref       = EXCLUDED.ref,
            highway   = EXCLUDED.highway,
            surface   = EXCLUDED.surface,
            tracktype = EXCLUDED.tracktype,
            maxspeed  = EXCLUDED.maxspeed,
            oneway    = EXCLUDED.oneway,
            oneway_direction = EXCLUDED.oneway_direction,
            node_ids  = EXCLUDED.node_ids,
            layer     = EXCLUDED.layer,
            bridge    = EXCLUDED.bridge,
            tunnel    = EXCLUDED.tunnel,
            lanes     = EXCLUDED.lanes,
            length_m  = EXCLUDED.length_m,
            geom      = EXCLUDED.geom,
            region    = EXCLUDED.region,
            scenic_eligible = EXCLUDED.scenic_eligible
    """
    # length_m computed with ::geography so it's a real spheroidal length in
    # meters, not degrees -- and we only decode the WKB hex once per row use.
    template = (
        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "ST_Length(ST_GeomFromWKB(decode(%s, 'hex'), 4326)::geography), "
        "ST_GeomFromWKB(decode(%s, 'hex'), 4326), %s, %s)"
    )
    rows = [
        (
            r["osm_id"], r["name"], r["ref"], r["highway"], r["surface"],
            r["tracktype"], r["maxspeed"], r["oneway_direction"] != 0,
            r["oneway_direction"], r["node_ids"], r["layer"], r["bridge"],
            r["tunnel"], r["lanes"], r["wkb_hex"], r["wkb_hex"], region,
            r["highway"] not in ("residential", "living_street"),
        )
        for r in roads
    ]
    psycopg2.extras.execute_values(cur, sql, rows, template=template, page_size=page_size)


def _insert_intersections(cur, intersections: list[dict], region: str, page_size: int) -> None:
    if not intersections:
        return

    sql = """
        INSERT INTO intersections (osm_id, kind, geom, region)
        VALUES %s
        ON CONFLICT (osm_id) DO NOTHING
    """
    template = "(%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s)"
    rows = [
        (i["osm_id"], i["kind"], i["lon"], i["lat"], region)
        for i in intersections
    ]
    psycopg2.extras.execute_values(cur, sql, rows, template=template, page_size=page_size)


def load(pbf_path: str, region: str, database_url: str, page_size: int = 5000) -> None:
    if not os.path.exists(pbf_path):
        raise FileNotFoundError(
            f"{pbf_path} not found. Download a regional extract from Geofabrik, "
            f"e.g. https://download.geofabrik.de/north-america/us/minnesota-latest.osm.pbf"
        )

    wkbfab = osmium.geom.WKBFactory()
    handler = RoadHandler(wkbfab)

    logger.info("Parsing %s (region=%s) ...", pbf_path, region)
    start = time.time()
    handler.apply_file(pbf_path, locations=True)
    elapsed = time.time() - start
    logger.info(
        "Parsed in %.1fs: %d candidate roads, %d intersections "
        "(skipped %d for missing location, %d too short)",
        elapsed, len(handler.roads), len(handler.intersections),
        handler._skipped_no_location, handler._skipped_too_short,
    )

    conn = psycopg2.connect(database_url)
    try:
        with conn:
            with conn.cursor() as cur:
                logger.info("Inserting roads ...")
                _insert_roads(cur, handler.roads, region, page_size)
                logger.info("Inserting intersections ...")
                _insert_intersections(cur, handler.intersections, region, page_size)
    except psycopg2.Error as exc:
        logger.error("Database error during load: %s", exc)
        raise
    finally:
        conn.close()

    logger.info("Done. %d roads and %d intersections loaded for region=%s.",
                len(handler.roads), len(handler.intersections), region)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", required=True, help="Path to a .osm.pbf extract")
    parser.add_argument("--region", required=True, help="Short region tag, e.g. 'minnesota'")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres connection string (defaults to $DATABASE_URL)",
    )
    parser.add_argument("--page-size", type=int, default=5000)
    args = parser.parse_args()

    if not args.database_url:
        logger.error("No database URL provided. Set DATABASE_URL or pass --database-url.")
        sys.exit(1)

    load(args.pbf, args.region, args.database_url, args.page_size)


if __name__ == "__main__":
    main()
