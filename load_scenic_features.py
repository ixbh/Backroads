"""Import explainable scenery signals from the same OSM extract as roads.

This is intentionally provider-independent and free of API keys. It imports
only signals we can name in the UI: water, forest, parks/protected land,
countryside land use, other natural land, and viewpoints.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import osmium
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("load_scenic_features")

BATCH_SIZE = 2000


def category_for(tags) -> str | None:
    if tags.get("tourism") == "viewpoint":
        return "viewpoint"
    if (
        tags.get("natural") in {"water", "bay", "coastline"}
        or tags.get("waterway") in {"river", "canal"}
        or tags.get("landuse") in {"reservoir", "basin"}
    ):
        return "water"
    if tags.get("natural") == "wood" or tags.get("landuse") == "forest":
        return "forest"
    if (
        tags.get("leisure") == "nature_reserve"
        or tags.get("boundary") == "protected_area"
    ):
        return "park"
    if tags.get("landuse") in {"farmland", "meadow", "orchard", "vineyard"}:
        return "countryside"
    if tags.get("natural") in {"wetland", "heath", "grassland", "cliff", "ridge", "peak"}:
        return "natural"
    return None


INSERT_SQL = """
    INSERT INTO scenic_features (osm_id, osm_type, category, name, geom, region)
    VALUES %s
    ON CONFLICT (osm_type, osm_id, category, region) DO UPDATE SET
        name = EXCLUDED.name, geom = EXCLUDED.geom, ingested_at = now()
"""
INSERT_TEMPLATE = (
    "(%s, %s, %s, %s, "
    "ST_SetSRID(ST_GeomFromWKB(decode(%s, 'hex')), 4326), %s)"
)


class ScenicFeatureHandler(osmium.SimpleHandler):
    def __init__(self, cursor, region: str):
        super().__init__()
        self.cursor = cursor
        self.region = region
        self.factory = osmium.geom.WKBFactory()
        self.pending: list[tuple] = []
        self.total = 0

    def _add(self, osm_id: int, osm_type: str, category: str, name: str | None, wkb_hex: str):
        self.pending.append((osm_id, osm_type, category, name, wkb_hex, self.region))
        if len(self.pending) >= BATCH_SIZE:
            self.flush()

    def flush(self):
        if not self.pending:
            return
        psycopg2.extras.execute_values(
            self.cursor, INSERT_SQL, self.pending, template=INSERT_TEMPLATE, page_size=BATCH_SIZE
        )
        self.total += len(self.pending)
        self.pending.clear()
        if self.total % 20_000 == 0:
            logger.info("Imported %d scenery features...", self.total)

    def node(self, node):
        category = category_for(node.tags)
        if category != "viewpoint":
            return
        try:
            self._add(node.id, "n", category, node.tags.get("name"), self.factory.create_point(node))
        except (osmium.InvalidLocationError, RuntimeError):
            return

    def way(self, way):
        category = category_for(way.tags)
        if not category:
            return
        try:
            self._add(way.id, "w", category, way.tags.get("name"), self.factory.create_linestring(way))
        except (osmium.InvalidLocationError, RuntimeError):
            return


def load(pbf_path: str, region: str, database_url: str, replace: bool = False):
    conn = psycopg2.connect(database_url)
    try:
        with conn:
            with conn.cursor() as cursor:
                if replace:
                    cursor.execute("DELETE FROM scenic_features WHERE region = %s", (region,))
                handler = ScenicFeatureHandler(cursor, region)
                handler.apply_file(pbf_path, locations=True)
                handler.flush()
                # Closed, simple land-use ways become polygons so roads inside
                # them receive the signal, not only roads near their boundary.
                cursor.execute(
                    """
                    UPDATE scenic_features
                    SET geom = ST_MakeValid(ST_MakePolygon(geom))
                    WHERE region = %s AND GeometryType(geom) = 'LINESTRING'
                      AND ST_IsClosed(geom) AND ST_IsSimple(geom) AND ST_NPoints(geom) >= 4
                    """,
                    (region,),
                )
        logger.info("Imported %d scenery features for region=%s.", handler.total, region)
    finally:
        conn.close()


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--replace", action="store_true", help="Replace existing features for this region")
    args = parser.parse_args()
    if not args.database_url:
        logger.error("No database URL provided. Set DATABASE_URL or pass --database-url.")
        sys.exit(1)
    load(args.pbf, args.region, args.database_url, args.replace)


if __name__ == "__main__":
    main()
