"""Runtime road-data providers for hosted, local, and desktop builds.

PostgreSQL/PostGIS remains the development and hosted source of truth. The
desktop beta uses a generated, read-only SQLite snapshot with an RTree index;
it never modifies or replaces the PostgreSQL database.
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
from pathlib import Path


DESKTOP_SCHEMA_VERSION = "1"
# PostgreSQL's PostGIS GiST `&&` index stores outward-rounded float bounding
# boxes. Match that harmless sub-meter tolerance so a road lying directly on
# the search envelope is not lost when the desktop RTree uses its own float
# representation.
BBOX_EPSILON_DEGREES = 0.000001


class RoadStoreConfigurationError(RuntimeError):
    pass


class SQLiteRoadStore:
    """Read candidate roads from an immutable desktop routing snapshot."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise RoadStoreConfigurationError(
                f"Desktop routing database was not found: {self.path}"
            )
        uri = f"{self.path.as_uri()}?mode=ro&immutable=1"
        self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._conn.execute("PRAGMA query_only = ON")
        version_row = self._conn.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        if not version_row or version_row[0] != DESKTOP_SCHEMA_VERSION:
            self.close()
            raise RoadStoreConfigurationError(
                "Desktop routing database has an unsupported schema version."
            )

    def fetch_candidate_roads(
        self,
        region: str,
        min_lon: float,
        min_lat: float,
        max_lon: float,
        max_lat: float,
    ) -> list[tuple]:
        rows = self._conn.execute(
            """
            SELECT r.id, r.name, r.highway, r.length_m, r.scenic_eligible,
                   r.oneway_direction, r.surface, r.tracktype, r.node_ids_json,
                   r.geom_wkb, r.curviness_score, r.urban_conflict_penalty,
                   r.scenery_score, r.scenery_signals_json
            FROM road_bounds b
            JOIN routing_roads r ON r.id = b.id
            WHERE r.region = ?
              AND b.min_lon <= ? AND b.max_lon >= ?
              AND b.min_lat <= ? AND b.max_lat >= ?
            """,
            (
                region,
                max_lon + BBOX_EPSILON_DEGREES,
                min_lon - BBOX_EPSILON_DEGREES,
                max_lat + BBOX_EPSILON_DEGREES,
                min_lat - BBOX_EPSILON_DEGREES,
            ),
        ).fetchall()
        return [
            (
                row[0], row[1], row[2], row[3], bool(row[4]), row[5],
                row[6], row[7], json.loads(row[8]) if row[8] else None,
                bytes(row[9]), row[10], row[11], row[12],
                json.loads(row[13]) if row[13] else None,
            )
            for row in rows
        ]

    def close(self) -> None:
        if getattr(self, "_conn", None) is not None:
            self._conn.close()
            self._conn = None


def open_road_source():
    """Open the configured runtime provider.

    BACKROADS_SQLITE_PATH explicitly selects the desktop snapshot. Otherwise
    DATABASE_URL selects PostgreSQL/PostGIS exactly as before.
    """
    sqlite_path = os.environ.get("BACKROADS_SQLITE_PATH")
    if sqlite_path:
        return SQLiteRoadStore(sqlite_path)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RoadStoreConfigurationError(
            "Set DATABASE_URL for PostgreSQL or BACKROADS_SQLITE_PATH for a desktop snapshot."
        )
    psycopg2 = importlib.import_module("psycopg2")
    return psycopg2.connect(database_url)
