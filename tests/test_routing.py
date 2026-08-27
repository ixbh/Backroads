import unittest

import networkx as nx
from shapely.geometry import LineString
from shapely import wkb

from routing import (
    RoadNetwork,
    RouteSegment,
    _connect_to_scenic_network,
    _edges_to_segments,
    _path_with_fallback,
    build_network,
    generate_exploration_loop,
    generate_point_to_point,
)


def add_edge(graph, a, b, road_id, *, scenic=True, both=True, length=100.0):
    for node in (a, b):
        graph.add_node(node, lon=node[0], lat=node[1])
    segment = RouteSegment(road_id, str(road_id), "secondary", length, 60, scenic, [a, b])
    graph.add_edge(a, b, length_m=length, cost=length, segment=segment)
    if both:
        reverse = RouteSegment(road_id, str(road_id), "secondary", length, 60, scenic, [b, a])
        graph.add_edge(b, a, length_m=length, cost=length, segment=reverse)


class RoutingTests(unittest.TestCase):
    def test_real_scenery_score_changes_parallel_road_selection(self):
        rows = [
            (1, "Lakeside", "secondary", 100.0, True, 0, "asphalt", None, [1, 2],
             wkb.dumps(LineString([(0, 0), (1, 0)])), 0, 1.0, 100, {"water": True}),
            (2, "Plain", "secondary", 100.0, True, 0, "asphalt", None, [1, 2],
             wkb.dumps(LineString([(0, 0), (1, 0)])), 0, 1.0, 0, {}),
        ]
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, *_): pass
            def fetchall(self): return rows
        class Connection:
            def cursor(self): return Cursor()

        network = build_network(
            Connection(), "test", 0, 0, 1000,
            {"curviness": 0, "traffic": 0, "city_avoidance": 0, "scenery": 1},
        )
        segments = _edges_to_segments(
            network.scenic, [("osm", 1), ("osm", 2)], "cost", set(), False
        )
        self.assertEqual(segments[0].road_id, 1)
        self.assertEqual(segments[0].scenery_signals, {"water": True})

    def test_fun_path_is_capped_when_detour_is_excessive(self):
        graph = nx.MultiDiGraph()
        start, middle, end = (0, 0), (0.005, 0.001), (0.01, 0)
        add_edge(graph, start, end, 1, length=1000)
        add_edge(graph, start, middle, 2, length=1000)
        add_edge(graph, middle, end, 3, length=1000)
        graph[start][middle][0]["cost"] = 100
        graph[middle][end][0]["cost"] = 100
        path, _, _ = _path_with_fallback(RoadNetwork(graph, graph), start, end)
        self.assertEqual(path, [start, end])

    def test_non_intersection_bends_remain_geometry_not_graph_nodes(self):
        rows = [(1, "Curve", "secondary", 200.0, True, 0, "asphalt", None, [1, 2, 3],
                 wkb.dumps(LineString([(0, 0), (0.5, 0.2), (1, 0)])), 70, 1.0, None, None)]
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, *_): pass
            def fetchall(self): return rows
        class Connection:
            def cursor(self): return Cursor()

        network = build_network(Connection(), "test", 0, 0, 1000, {})
        self.assertEqual(network.scenic.number_of_nodes(), 2)
        self.assertEqual(network.scenic[("osm", 1)][("osm", 3)][0]["segment"].coords,
                         [(0.0, 0.0), (0.5, 0.2), (1.0, 0.0)])

    def test_city_avoidance_strongly_penalizes_high_conflict_roads(self):
        rows = [
            (1, "Quiet", "secondary", 100.0, True, 0, "asphalt", None, [1, 2],
             wkb.dumps(LineString([(0, 0), (1, 0)])), 50, 1.0, None, None),
            (2, "Busy", "secondary", 100.0, True, 0, "asphalt", None, [3, 4],
             wkb.dumps(LineString([(0, 1), (1, 1)])), 50, 0.1, None, None),
        ]

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, *_): pass
            def fetchall(self): return rows
        class Connection:
            def cursor(self): return Cursor()

        network = build_network(
            Connection(), "test", 0, 0, 1000,
            {"curviness": 0, "traffic": 0, "city_avoidance": 1},
        )
        quiet_cost = network.scenic[("osm", 1)][("osm", 2)][0]["cost"]
        busy_cost = network.scenic[("osm", 3)][("osm", 4)][0]["cost"]
        self.assertGreater(busy_cost, quiet_cost * 5)

    def test_build_network_splits_at_intermediate_vertices_and_honors_oneway(self):
        rows = [
            (1, "Main", "secondary", 200.0, True, 1, "asphalt", None, [1, 2, 3],
             wkb.dumps(LineString([(0, 0), (1, 0), (2, 0)])), 50, 1.0, None, None),
            (2, "Cross", "secondary", 100.0, True, 0, "asphalt", None, [4, 2],
             wkb.dumps(LineString([(1, -1), (1, 0)])), 50, 1.0, None, None),
        ]

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, *_): pass
            def fetchall(self): return rows

        class Connection:
            def cursor(self): return Cursor()

        network = build_network(Connection(), "test", 1, 0, 1000, {})
        self.assertTrue(nx.has_path(network.scenic, ("osm", 4), ("osm", 3)))
        self.assertFalse(nx.has_path(network.scenic, ("osm", 3), ("osm", 1)))

    def test_matching_coordinates_with_different_osm_nodes_are_not_an_intersection(self):
        rows = [
            (1, "Overpass", "secondary", 200.0, True, 0, "asphalt", None, [10, 11, 12],
             wkb.dumps(LineString([(0, 1), (1, 1), (2, 1)])), 20, 1.0, None, None),
            (2, "Road below", "secondary", 200.0, True, 0, "asphalt", None, [20, 21, 22],
             wkb.dumps(LineString([(1, 0), (1, 1), (1, 2)])), 20, 1.0, None, None),
        ]
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, *_): pass
            def fetchall(self): return rows
        class Connection:
            def cursor(self): return Cursor()

        network = build_network(Connection(), "test", 1, 1, 1000, {})
        self.assertFalse(nx.has_path(network.scenic, ("osm", 10), ("osm", 20)))

    def test_paved_only_excludes_known_unpaved_and_unknown_tracks(self):
        rows = [
            (1, "Gravel", "secondary", 100.0, True, 0, "gravel", None, [1, 2],
             wkb.dumps(LineString([(0, 0), (1, 0)])), 20, 1.0, None, None),
            (2, "Unknown track", "track", 100.0, True, 0, None, "grade1", [1, 2],
             wkb.dumps(LineString([(0, 0), (1, 0)])), 20, 1.0, None, None),
            (3, "Paved", "secondary", 100.0, True, 0, "asphalt", None, [1, 2],
             wkb.dumps(LineString([(0, 0), (1, 0)])), 20, 1.0, None, None),
        ]
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, *_): pass
            def fetchall(self): return rows
        class Connection:
            def cursor(self): return Cursor()

        network = build_network(Connection(), "test", 0, 0, 1000, {"paved_only": True})
        road_ids = {data["segment"].road_id for *_, data in network.scenic.edges(data=True)}
        self.assertEqual(road_ids, {3})

    def test_reverse_oneway_uses_only_opposite_geometry_direction(self):
        rows = [
            (1, "Reverse", "secondary", 100.0, True, -1, "asphalt", None, [1, 2],
             wkb.dumps(LineString([(0, 0), (1, 0)])), 20, 1.0, None, None),
        ]
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, *_): pass
            def fetchall(self): return rows
        class Connection:
            def cursor(self): return Cursor()

        network = build_network(Connection(), "test", 0, 0, 1000, {})
        self.assertTrue(network.scenic.has_edge(("osm", 2), ("osm", 1)))
        self.assertFalse(network.scenic.has_edge(("osm", 1), ("osm", 2)))

    def test_segment_geometry_follows_directed_path(self):
        graph = nx.MultiDiGraph()
        add_edge(graph, (0, 0), (1, 0), 1, both=True)
        segments = _edges_to_segments(graph, [(1, 0), (0, 0)], "cost", set(), False)
        self.assertEqual(segments[0].coords, [(1, 0), (0, 0)])

    def test_origin_connector_respects_one_way_direction(self):
        full, scenic = nx.MultiDiGraph(), nx.MultiDiGraph()
        pin, junction, scenic_node = (0, 0), (1, 0), (2, 0)
        add_edge(full, pin, junction, 1, scenic=False, both=False)
        add_edge(full, junction, scenic_node, 2, scenic=True, both=False)
        add_edge(scenic, junction, scenic_node, 2, both=False)
        entry, connector = _connect_to_scenic_network(RoadNetwork(full, scenic), *pin)
        self.assertEqual(entry, junction)
        self.assertEqual([s.road_id for s in connector], [1])

    def test_point_to_point_returns_contiguous_oriented_geometry(self):
        full, scenic = nx.MultiDiGraph(), nx.MultiDiGraph()
        nodes = [(0, 0), (1, 0), (2, 0), (3, 0)]
        add_edge(full, nodes[0], nodes[1], 1, scenic=False)
        add_edge(full, nodes[1], nodes[2], 2)
        add_edge(full, nodes[2], nodes[3], 3, scenic=False)
        add_edge(scenic, nodes[1], nodes[2], 2)
        route = generate_point_to_point(RoadNetwork(full, scenic), 0, 0, 3, 0)
        self.assertIsNotNone(route)
        self.assertEqual([s.road_id for s in route.segments], [1, 2, 3])
        for left, right in zip(route.segments, route.segments[1:]):
            self.assertEqual(left.coords[-1], right.coords[0])

    def test_one_way_destination_connector_is_not_reversed_illegally(self):
        full, scenic = nx.MultiDiGraph(), nx.MultiDiGraph()
        start, exit_node, destination = (0, 0), (1, 0), (2, 0)
        add_edge(full, start, exit_node, 1)
        add_edge(scenic, start, exit_node, 1)
        add_edge(full, exit_node, destination, 2, scenic=False, both=False)
        route = generate_point_to_point(RoadNetwork(full, scenic), 0, 0, 2, 0)
        self.assertIsNotNone(route)
        self.assertEqual(route.segments[-1].coords, [exit_node, destination])

    def test_exploration_loop_visits_focus_area_and_returns_to_start(self):
        graph = nx.MultiDiGraph()
        nodes = [(0, 0), (1, 0), (1, 1), (0, 1)]
        for index, (a, b) in enumerate(zip(nodes, nodes[1:] + nodes[:1]), 1):
            add_edge(graph, a, b, index)
        route = generate_exploration_loop(
            RoadNetwork(graph, graph), 0, 0, 1, 1, 4000,
        )
        self.assertIsNotNone(route)
        self.assertEqual(route.segments[0].coords[0], (0, 0))
        self.assertEqual(route.segments[-1].coords[-1], (0, 0))
        self.assertTrue(any((1, 1) in segment.coords for segment in route.segments))

    def test_exploration_ignores_nearer_nodes_in_disconnected_boundary_fragment(self):
        graph = nx.MultiDiGraph()
        component = [(0, 0), (0.01, 0), (0.01, 0.01), (0, 0.01)]
        for index, (a, b) in enumerate(zip(component, component[1:] + component[:1]), 1):
            add_edge(graph, a, b, index)
        add_edge(graph, (0.15, 0), (0.16, 0), 99)
        route = generate_exploration_loop(
            RoadNetwork(graph, graph), 0, 0, 0.01, 0, 100_000,
        )
        self.assertIsNotNone(route)
        self.assertNotIn(99, [segment.road_id for segment in route.segments])


if __name__ == "__main__":
    unittest.main()
