"""Export a compact, read-only desktop routing snapshot from PostgreSQL.

The source database is queried only. Output is written to a temporary SQLite
file and atomically replaced after integrity checks succeed.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

from road_store import DESKTOP_SCHEMA_VERSION


SCHEMA_SQL = """
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE routing_roads (
    id INTEGER PRIMARY KEY,
    region TEXT NOT NULL,
    name TEXT,
    highway TEXT NOT NULL,
    length_m REAL NOT NULL,
    scenic_eligible INTEGER NOT NULL,
    oneway_direction INTEGER NOT NULL,
    surface TEXT,
    tracktype TEXT,
    node_ids_json TEXT,
    geom_wkb BLOB NOT NULL,
    curviness_score INTEGER,
    urban_conflict_penalty REAL,
    scenery_score INTEGER,
    scenery_signals_json TEXT
);

CREATE INDEX routing_roads_region_idx ON routing_roads(region);
CREATE VIRTUAL TABLE road_bounds USING rtree(
    id,
    min_lon, max_lon,
    min_lat, max_lat
);
"""


SELECT_SQL = """
SELECT r.id, r.region, r.name, r.highway, r.length_m,
       r.scenic_eligible, r.oneway_direction, r.surface, r.tracktype,
       array_to_json(r.node_ids)::text,
       ST_AsBinary(r.geom),
       rs.curviness_score, rs.urban_conflict_penalty,
       rs.scenery_score, rs.scenery_signals::text,
       ST_XMin(Box3D(r.geom)), ST_XMax(Box3D(r.geom)),
       ST_YMin(Box3D(r.geom)), ST_YMax(Box3D(r.geom))
FROM roads r
LEFT JOIN road_scores rs ON rs.road_id = r.id
WHERE r.region = %s
ORDER BY r.id
"""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="minnesota")
    parser.add_argument(
        "--output", type=Path, default=Path("data/backroads.sqlite3")
    )
    parser.add_argument(
        "--database-url",
        help="PostgreSQL connection URL. Defaults to DATABASE_URL from .env.",
    )
    parser.add_argument("--batch-size", type=int, default=5000)
    return parser.parse_args()


def export_snapshot(
    database_url: str,
    region: str,
    output: Path,
    batch_size: int = 5000,
) -> tuple[int, int]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.name}.building")
    if temporary.exists():
        temporary.unlink()

    exported = 0
    scored = 0
    try:
        sqlite_conn = sqlite3.connect(temporary)
        sqlite_conn.execute("PRAGMA journal_mode = OFF")
        sqlite_conn.execute("PRAGMA synchronous = OFF")
        sqlite_conn.execute("PRAGMA temp_store = MEMORY")
        sqlite_conn.execute("PRAGMA locking_mode = EXCLUSIVE")
        sqlite_conn.executescript(SCHEMA_SQL)
        sqlite_conn.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            [
                ("schema_version", DESKTOP_SCHEMA_VERSION),
                ("region", region),
                ("source", "OpenStreetMap contributors"),
                ("license", "ODbL-1.0"),
            ],
        )

        with psycopg2.connect(database_url) as postgres_conn:
            with postgres_conn.cursor(name="desktop_export") as cursor:
                cursor.itersize = batch_size
                cursor.execute(SELECT_SQL, (region,))
                while True:
                    batch = cursor.fetchmany(batch_size)
                    if not batch:
                        break
                    road_rows = []
                    bounds_rows = []
                    for row in batch:
                        road_rows.append(
                            (
                                row[0], row[1], row[2], row[3], row[4],
                                int(row[5]), row[6], row[7], row[8], row[9],
                                bytes(row[10]), row[11], row[12], row[13],
                                row[14],
                            )
                        )
                        bounds_rows.append((row[0], row[15], row[16], row[17], row[18]))
                        scored += int(row[11] is not None or row[13] is not None)
                    sqlite_conn.executemany(
                        """
                        INSERT INTO routing_roads VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        road_rows,
                    )
                    sqlite_conn.executemany(
                        "INSERT INTO road_bounds VALUES (?, ?, ?, ?, ?)",
                        bounds_rows,
                    )
                    sqlite_conn.commit()
                    exported += len(batch)
                    print(f"Exported {exported:,} roads...", flush=True)

        sqlite_conn.execute("ANALYZE")
        sqlite_conn.execute("PRAGMA optimize")
        sqlite_conn.commit()
        integrity = sqlite_conn.execute("PRAGMA integrity_check").fetchone()[0]
        stored = sqlite_conn.execute("SELECT count(*) FROM routing_roads").fetchone()[0]
        indexed = sqlite_conn.execute("SELECT count(*) FROM road_bounds").fetchone()[0]
        sqlite_conn.close()
        if integrity != "ok" or stored != exported or indexed != exported:
            raise RuntimeError(
                f"Snapshot verification failed: integrity={integrity}, roads={stored}, bounds={indexed}"
            )
        os.replace(temporary, output)
        return exported, scored
    except Exception:
        try:
            sqlite_conn.close()
        except (NameError, sqlite3.Error):
            pass
        if temporary.exists():
            temporary.unlink()
        raise


def main() -> None:
    load_dotenv()
    args = _parse_args()
    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not set and --database-url was not provided.")
    roads, scored = export_snapshot(
        database_url, args.region, args.output, args.batch_size
    )
    size_mib = args.output.resolve().stat().st_size / 1024 / 1024
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "region": args.region,
                "roads": roads,
                "scored_roads": scored,
                "size_mib": round(size_mib, 1),
            }
        )
    )


if __name__ == "__main__":
    main()
