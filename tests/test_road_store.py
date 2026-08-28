import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shapely import wkb
from shapely.geometry import LineString

from road_store import SQLiteRoadStore, open_road_source
from routing import build_network


class SQLiteRoadStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "roads.sqlite3"
        conn = sqlite3.connect(self.database_path)
        try:
            conn.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO metadata VALUES ('schema_version', '1');
                CREATE TABLE routing_roads (
                    id INTEGER PRIMARY KEY, region TEXT, name TEXT, highway TEXT,
                    length_m REAL, scenic_eligible INTEGER, oneway_direction INTEGER,
                    surface TEXT, tracktype TEXT, node_ids_json TEXT, geom_wkb BLOB,
                    curviness_score INTEGER, urban_conflict_penalty REAL,
                    scenery_score INTEGER, scenery_signals_json TEXT
                );
                CREATE VIRTUAL TABLE road_bounds USING rtree(
                    id, min_lon, max_lon, min_lat, max_lat
                );
                """
            )
            conn.execute(
                "INSERT INTO routing_roads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    1, "minnesota", "Lakeside", "secondary", 100.0, 1, 0,
                    "asphalt", None, json.dumps([10, 11]),
                    wkb.dumps(LineString([(-93.1, 44.8), (-93.09, 44.8)])),
                    70, 0.95, 80, json.dumps({"water": True}),
                ),
            )
            conn.execute(
                "INSERT INTO road_bounds VALUES (?, ?, ?, ?, ?)",
                (1, -93.1, -93.09, 44.8, 44.8),
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.temporary.cleanup()

    def test_fetches_only_roads_overlapping_requested_bounds(self):
        store = SQLiteRoadStore(self.database_path)
        try:
            rows = store.fetch_candidate_roads(
                "minnesota", -93.11, 44.79, -93.08, 44.81
            )
            outside = store.fetch_candidate_roads(
                "minnesota", -92.0, 44.0, -91.0, 45.0
            )
        finally:
            store.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][8], [10, 11])
        self.assertEqual(rows[0][13], {"water": True})
        self.assertEqual(outside, [])

    def test_sqlite_rows_build_the_same_routing_graph_contract(self):
        store = SQLiteRoadStore(self.database_path)
        try:
            network = build_network(
                store, "minnesota", -93.095, 44.8, 5000,
                {"paved_only": True},
            )
        finally:
            store.close()

        self.assertTrue(network.scenic.has_edge(("osm", 10), ("osm", 11)))
        segment = network.scenic[("osm", 10)][("osm", 11)][0]["segment"]
        self.assertEqual(segment.road_id, 1)
        self.assertEqual(segment.scenery_signals, {"water": True})

    def test_environment_selects_desktop_store_without_postgres(self):
        with patch.dict(
            os.environ,
            {"BACKROADS_SQLITE_PATH": str(self.database_path)},
            clear=True,
        ):
            store = open_road_source()
        try:
            self.assertIsInstance(store, SQLiteRoadStore)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
