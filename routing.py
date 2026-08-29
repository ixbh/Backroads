"""
Phase 5: Route generation.

Turns the scored road network (Phase 2 geometry + Phase 3 curviness/urban
scores, Phase 4 scenery once it exists) into an actual driveable route that
respects the user's stated preferences -- either a loop back to a start
point, or a route between a start and a destination.

IMPORTANT DESIGN POINT (added after real testing surfaced a real problem):
residential streets and living_streets are NOT just discouraged, they are
completely excluded from the "fun route" pathfinding. The only place a
residential road can appear at all is a short CONNECTOR segment linking the
user's literal start/destination pin to the nearest point on the real
(non-residential) road network -- because most starting points are, in
reality, someone's driveway. Those connector segments are clearly flagged
(`is_connector=True`) in the output. The actual recommended route body never
routes through a neighborhood.

How the "fun road" pathfinding works, in plain terms (see architecture doc
section 7 for the full reasoning): each edge gets an "adjusted cost" that is
NOT just its length -- it's discounted based on how well it matches the
user's preferences (curvy, low-conflict). A fun road effectively looks
shorter to the pathfinder than it really is, so ordinary shortest-path
search naturally prefers it over a boring one, without needing a from-scratch
orienteering solver (NP-hard in general).
"""

from __future__ import annotations

import logging
import math
import random
from collections import Counter
from dataclasses import dataclass, field, replace

import networkx as nx
from shapely import wkb as shapely_wkb

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("routing")

# How much a road's desirability can discount its effective length within
# the scenic (non-residential) network. 0.85 means a maximally-desirable
# road looks 85% shorter to the pathfinder -- strong enough to route around
# a boring direct path, but never actually free.
MAX_DISCOUNT = 0.85

# If (and only if) the scenic-only network turns out to be genuinely
# disconnected between two points a route needs to link -- a real thing:
# two good rural roads that only connect to each other through a small
# town's residential grid -- the fallback search is allowed to use
# residential roads, but makes them look this many times more "expensive"
# than they really are, so the absolute minimum amount of residential
# street gets used, and only when there is truly no alternative.
FALLBACK_PENALTY = 8.0

# `urban_conflict_penalty` is 1 on quiet roads and approaches 0 as nearby
# signals/stops per kilometre rise. City avoidance turns that signal into a
# strong cost multiplier instead of merely removing a scenic discount.
CITY_CONFLICT_MAX_PENALTY = 5.0
CITY_NETWORK_MAX_PENALTY = 3.0
ROAD_DENSITY_CELL_DEG = 0.02  # roughly 1.5 x 2.2 km around Minnesota

# OSM surface coverage is incomplete, so "paved only" can be a hard ban on
# explicitly unpaved roads while unknown regular roads remain usable and are
# disclosed in the response. Tracks are riskier and require an explicit paved
# surface. `compacted` and `fine_gravel` are intentionally treated as unpaved
# for low-clearance cars even though they can be smooth when maintained.
KNOWN_PAVED_SURFACES = {
    "asphalt", "paved", "concrete", "concrete:lanes", "concrete:plates",
    "paving_stones", "sett", "cobblestone", "bricks", "chipseal",
}
KNOWN_UNPAVED_SURFACES = {
    "unpaved", "gravel", "fine_gravel", "dirt", "earth", "ground", "mud",
    "sand", "grass", "grass_paver", "compacted", "pebblestone", "woodchips",
    "clay", "natural", "wood",
}

# A preferred road is allowed to make a leg somewhat longer, but never turn
# a local drive into a cross-region excursion. If the fun-cost path exceeds
# this multiple of the distance-shortest path, use the shorter path.
MAX_LEG_STRETCH = 1.4
# Preference-aware waypoints already pull the loop toward interesting roads.
# Keep each leg's additional path detour modest so a 95-mile request does not
# quietly become a 130-mile drive just because the curve slider is high.
MAX_PREFERENCE_STRETCH_BONUS = 0.15
LANDMARK_SIGNALS = {"park", "viewpoint", "attraction", "monument"}


@dataclass(slots=True)
class RouteSegment:
    road_id: int
    name: str | None
    highway: str
    length_m: float
    curviness_score: int | None
    scenic_eligible: bool
    coords: list  # [(lon, lat), ...]
    surface: str | None = None
    tracktype: str | None = None
    is_connector: bool = False  # last-mile link to/from the exact pin, not part of "the fun route"
    scenery_score: int | None = None
    scenery_signals: dict = field(default_factory=dict)


def _reversed_segment(segment: RouteSegment) -> RouteSegment:
    """Return a traversal-safe copy of a segment in the opposite direction."""
    return replace(segment, coords=list(reversed(segment.coords)))


@dataclass
class GeneratedRoute:
    segments: list = field(default_factory=list)
    total_length_m: float = 0.0

    def to_geojson(self) -> dict:
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "road_id": s.road_id,
                        "name": s.name,
                        "highway": s.highway,
                        "surface": s.surface,
                        "tracktype": s.tracktype,
                        "length_m": round(s.length_m),
                        "curviness_score": s.curviness_score,
                        "scenery_score": s.scenery_score,
                        "scenery_signals": s.scenery_signals,
                        "scenic_eligible": s.scenic_eligible,
                        "is_connector": s.is_connector,
                    },
                    "geometry": {"type": "LineString", "coordinates": s.coords},
                }
                for s in self.segments
            ],
        }


def _node_key(lon: float, lat: float, precision: int = 6):
    """Fallback identity for legacy rows imported without OSM node IDs."""
    return ("coord", round(lon, precision), round(lat, precision))


def _surface_allowed(highway: str, surface: str | None, paved_only: bool) -> bool:
    if not paved_only:
        return True
    normalized = (surface or "").strip().lower()
    if normalized in KNOWN_UNPAVED_SURFACES:
        return False
    # A generic highway=track is not safe to assume suitable for a lowered
    # car. Only retain one when OSM explicitly labels its surface as paved.
    if highway == "track" and normalized not in KNOWN_PAVED_SURFACES:
        return False
    return True


class RoadNetwork:
    """Holds two views of the same loaded roads:
      - full: every road, including residential -- used ONLY to find the
        shortest possible connector between an arbitrary pin and the real
        road network.
      - scenic: residential/living_street removed entirely -- used for all
        actual "fun route" pathfinding. Nothing in here is a neighborhood
        street, full stop.
    """

    def __init__(self, full, scenic, weights=None):
        self.full = full
        self.scenic = scenic
        self.weights = weights or {}


def build_network(
    conn,
    region: str,
    center_lon: float,
    center_lat: float,
    radius_m: float,
    weights: dict,
) -> RoadNetwork:
    """Loads roads within radius_m of the center point into both graph views.

    `weights` expects preference values in [0, 1] plus `paved_only`.
    """
    lat_buffer_deg = radius_m / 111_320.0
    lon_buffer_deg = radius_m / (111_320.0 * math.cos(math.radians(center_lat)))

    bounds = (
        center_lon - lon_buffer_deg, center_lat - lat_buffer_deg,
        center_lon + lon_buffer_deg, center_lat + lat_buffer_deg,
    )
    if hasattr(conn, "fetch_candidate_roads"):
        rows = conn.fetch_candidate_roads(region, *bounds)
    else:
        # Compatibility path for PostgreSQL connections and the lightweight
        # cursor fakes used by focused routing tests.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.id, r.name, r.highway, r.length_m, r.scenic_eligible,
                       r.oneway_direction, r.surface, r.tracktype, r.node_ids,
                       ST_AsBinary(r.geom) AS geom_wkb,
                       rs.curviness_score, rs.urban_conflict_penalty,
                       rs.scenery_score, rs.scenery_signals
                FROM roads r
                LEFT JOIN road_scores rs ON rs.road_id = r.id
                WHERE r.region = %s
                  AND r.geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                """,
                (region, *bounds),
            )
            rows = cur.fetchall()

    logger.info("Loaded %d candidate roads within ~%.0fm of center.", len(rows), radius_m)

    # Direction matters: OSM one-way restrictions are safety constraints, not
    # preferences. Bidirectional roads are represented by one edge each way.
    full = nx.MultiDiGraph()
    # Keep one canonical graph instead of duplicating every scenic node, edge,
    # attribute dictionary, and RouteSegment into a second MultiDiGraph.  The
    # scenic graph becomes an edge-induced view after construction.  This is
    # materially faster and smaller on low-CPU hosted instances while
    # preserving the exact same pathfinding topology and weights.
    scenic_edges = []
    curviness_weight = weights.get("curviness", 0.5)
    traffic_weight = weights.get("traffic", 0.5)
    city_avoidance = weights.get("city_avoidance", 0.75)
    scenery_weight = weights.get("scenery", 0.5)
    landmark_weight = weights.get("landmarks", 0.0)
    paved_only = weights.get("paved_only", True)

    # Count exact OSM node identities first. Coordinate matching is retained
    # only as a legacy fallback for rows not yet refreshed from the PBF. Node
    # identity is what keeps a bridge and the road under it disconnected.
    parsed_rows = []
    node_counts = Counter()
    for row in rows:
        if not _surface_allowed(row[2], row[6], paved_only):
            continue
        coords = list(shapely_wkb.loads(bytes(row[9])).coords)
        if len(coords) < 2 or row[3] <= 0:
            continue
        osm_node_ids = row[8]
        if osm_node_ids and len(osm_node_ids) == len(coords):
            keys = [("osm", int(node_id)) for node_id in osm_node_ids]
        else:
            keys = [_node_key(*coord) for coord in coords]
        node_counts.update(keys)
        parsed_rows.append((*row[:8], coords, keys, *row[10:]))
    # The parsed representation now owns everything graph construction needs.
    # Do not retain the original DB result tuples and WKB buffers alongside it.
    del rows

    # Count actual routing nodes per small grid cell. Dense metro grids have
    # many more endpoints/intersections than rural roads or small towns. Use
    # robust regional percentiles plus an absolute floor so a rural-only
    # search does not mislabel its busiest crossroads as a dense city.
    routing_node_keys = set()
    for row in parsed_rows:
        coords, keys = row[8], row[9]
        routing_node_keys.add((keys[0], coords[0]))
        routing_node_keys.add((keys[-1], coords[-1]))
        routing_node_keys.update(
            (key, coord) for key, coord in zip(keys[1:-1], coords[1:-1])
            if node_counts[key] > 1
        )
    density_cells = Counter(
        (math.floor(lon / ROAD_DENSITY_CELL_DEG), math.floor(lat / ROAD_DENSITY_CELL_DEG))
        for _, (lon, lat) in routing_node_keys
    )
    del routing_node_keys
    density_values = sorted(density_cells.values())
    if density_values:
        density_low = max(20, density_values[int((len(density_values) - 1) * 0.75)])
        density_high = max(density_low + 20, density_values[int((len(density_values) - 1) * 0.97)])
    else:
        density_low, density_high = 20, 40
    del density_values
    logger.info("Road-density city proxy: quiet <= %d nodes/cell; dense >= %d.", density_low, density_high)

    # Clear each parsed row as it is consumed. This keeps temporary geometry
    # and node-id arrays from overlapping the complete finished graph at peak.
    for row_index in range(len(parsed_rows)):
        road_id, name, highway, length_m, scenic_eligible, oneway_direction, surface, tracktype, coords, keys, curviness_score, urban_penalty, scenery_score, scenery_signals = parsed_rows[row_index]
        parsed_rows[row_index] = None
        curviness_norm = (curviness_score or 0) / 100.0
        conflict_norm = urban_penalty if urban_penalty is not None else 1.0
        scenery_norm = (scenery_score or 0) / 100.0
        landmark_norm = max(
            (
                1.0 if scenery_signals.get("attraction") or scenery_signals.get("monument")
                else 0.85 if scenery_signals.get("viewpoint")
                else 0.65 if scenery_signals.get("park")
                else 0.0
            ) if scenery_signals else 0.0,
            0.0,
        )
        effective_scenery_weight = scenery_weight if scenery_score is not None else 0.0
        desirability = (
            curviness_norm * curviness_weight
            + conflict_norm * traffic_weight
            + scenery_norm * effective_scenery_weight
            + landmark_norm * landmark_weight
        )
        max_possible = (
            curviness_weight + traffic_weight + effective_scenery_weight + landmark_weight
        )
        discount = MAX_DISCOUNT * (desirability / max_possible) if max_possible > 0 else 0.0
        urban_cost_multiplier = 1.0 + (
            city_avoidance * (1.0 - conflict_norm) * CITY_CONFLICT_MAX_PENALTY
        )
        midpoint = coords[len(coords) // 2]
        cell = (
            math.floor(midpoint[0] / ROAD_DENSITY_CELL_DEG),
            math.floor(midpoint[1] / ROAD_DENSITY_CELL_DEG),
        )
        density_norm = max(0.0, min(1.0, (density_cells[cell] - density_low) / (density_high - density_low)))
        density_cost_multiplier = 1.0 + city_avoidance * density_norm * CITY_NETWORK_MAX_PENALTY
        adjusted_cost = (
            length_m * (1.0 - discount) * urban_cost_multiplier * density_cost_multiplier
        )

        # Split only at endpoints and shared OSM intersection vertices.
        chord_lengths = [
            math.hypot(
                (b[0] - a[0]) * 111_320.0 * math.cos(math.radians((a[1] + b[1]) / 2)),
                (b[1] - a[1]) * 111_320.0,
            )
            for a, b in zip(coords, coords[1:])
        ]
        chord_total = sum(chord_lengths)
        if chord_total <= 0:
            continue

        split_indices = [0] + [
            index for index in range(1, len(coords) - 1)
            if node_counts[keys[index]] > 1
        ] + [len(coords) - 1]
        for start_index, end_index in zip(split_indices, split_indices[1:]):
            piece_coords = coords[start_index:end_index + 1]
            a, b = piece_coords[0], piece_coords[-1]
            chord_length = sum(chord_lengths[start_index:end_index])
            u, v = keys[start_index], keys[end_index]
            if u == v or chord_length <= 0:
                continue
            piece_length = length_m * chord_length / chord_total
            piece_cost = adjusted_cost * chord_length / chord_total
            segment = RouteSegment(
                road_id=road_id, name=name, highway=highway, length_m=piece_length,
                curviness_score=curviness_score, scenic_eligible=scenic_eligible,
                coords=piece_coords, surface=surface, tracktype=tracktype,
                scenery_score=scenery_score,
                scenery_signals=scenery_signals or {},
            )
            full.add_node(u, lon=a[0], lat=a[1])
            full.add_node(v, lon=b[0], lat=b[1])
            full_cost = piece_cost if scenic_eligible else piece_cost * FALLBACK_PENALTY
            if oneway_direction >= 0:
                edge_key = full.add_edge(
                    u, v, length_m=piece_length, cost=full_cost,
                    urban_density=density_norm, landmark_score=landmark_norm,
                    segment=segment, reversed=False,
                )
                if scenic_eligible:
                    scenic_edges.append((u, v, edge_key))
            if oneway_direction <= 0:
                edge_key = full.add_edge(
                    v, u, length_m=piece_length, cost=full_cost,
                    urban_density=density_norm, landmark_score=landmark_norm,
                    segment=segment, reversed=True,
                )
                if scenic_eligible:
                    scenic_edges.append((v, u, edge_key))

    # edge_subgraph is a read-only view backed by `full`: it contains only
    # nodes incident to scenic-eligible edges and does not copy graph data.
    scenic = full.edge_subgraph(scenic_edges)
    del scenic_edges, parsed_rows, node_counts, density_cells

    logger.info(
        "Network built: %d full nodes / %d full edges; %d scenic-only nodes / %d scenic-only edges.",
        full.number_of_nodes(), full.number_of_edges(),
        scenic.number_of_nodes(), scenic.number_of_edges(),
    )
    return RoadNetwork(full, scenic, weights)


def _nearest_node(graph, lon: float, lat: float, candidates: set | None = None):
    best, best_dist = None, float("inf")
    for n, data in graph.nodes(data=True):
        if candidates is not None and n not in candidates:
            continue
        d = (data["lon"] - lon) ** 2 + (data["lat"] - lat) ** 2
        if d < best_dist:
            best, best_dist = n, d
    return best


def _safe_waypoint_nodes(graph, candidates: set | None = None) -> set:
    """Return intersections that are unlikely to terminate on a road spur.

    Route geometry only creates graph nodes at road endpoints and real OSM
    intersections. Requiring at least two distinct neighbours removes the
    cul-de-sac/end-of-road anchors responsible for most out-and-back hairs.
    """
    pool = candidates if candidates is not None else graph.nodes
    safe = set()
    for node in pool:
        if node not in graph:
            continue
        neighbours = set(graph.successors(node))
        neighbours.update(graph.predecessors(node))
        if len(neighbours) >= 2 and graph.out_degree(node) and graph.in_degree(node):
            safe.add(node)
    return safe or set(pool)


def _node_preference_quality(graph, node, weights: dict) -> float:
    edge_values = []
    for *_edge, data in graph.out_edges(node, data=True):
        segment = data["segment"]
        landmark = data.get("landmark_score", 0.0)
        positive = (
            ((segment.curviness_score or 0) / 100.0) * weights.get("curviness", 0.0)
            + ((segment.scenery_score or 0) / 100.0) * weights.get("scenery", 0.0)
            + landmark * weights.get("landmarks", 0.0)
            + (1.0 - data.get("urban_density", 0.0)) * weights.get("city_avoidance", 0.0)
        )
        scale = (
            weights.get("curviness", 0.0) + weights.get("scenery", 0.0)
            + weights.get("landmarks", 0.0) + weights.get("city_avoidance", 0.0)
        )
        edge_values.append(positive / scale if scale else 0.0)
    return max(edge_values, default=0.0)


def _nearest_preferred_node(
    graph, lon: float, lat: float, *, candidates: set | None = None,
    weights: dict | None = None, preference_reach_m: float = 2500.0,
):
    """Snap near a geometric target while favouring genuinely desirable roads."""
    pool = candidates if candidates is not None else graph.nodes
    nearest, nearest_m = None, float("inf")
    cos_lat = math.cos(math.radians(lat))
    for node in pool:
        data = graph.nodes[node]
        distance_m = math.hypot(
            (data["lon"] - lon) * 111_320.0 * cos_lat,
            (data["lat"] - lat) * 111_320.0,
        )
        if distance_m < nearest_m:
            nearest, nearest_m = node, distance_m
    if nearest is None or not weights:
        return nearest

    best, best_objective = nearest, nearest_m
    max_snap_m = nearest_m + max(800.0, preference_reach_m)
    for node in pool:
        data = graph.nodes[node]
        distance_m = math.hypot(
            (data["lon"] - lon) * 111_320.0 * cos_lat,
            (data["lat"] - lat) * 111_320.0,
        )
        if distance_m > max_snap_m:
            continue
        quality = _node_preference_quality(graph, node, weights)
        objective = distance_m - preference_reach_m * 0.75 * quality
        if objective < best_objective:
            best, best_objective = node, objective
    return best


def _remove_immediate_retracing(segments: list[RouteSegment]) -> list[RouteSegment]:
    """Cancel exact A→B, B→A road retraces, including multi-edge hairs."""
    cleaned: list[RouteSegment] = []
    for segment in segments:
        if cleaned:
            previous = cleaned[-1]
            if (
                previous.road_id == segment.road_id
                and previous.coords[0] == segment.coords[-1]
                and previous.coords[-1] == segment.coords[0]
            ):
                cleaned.pop()
                continue
        cleaned.append(segment)
    return cleaned


def _edges_to_segments(graph, path, weight_key: str, seen_edges: set, is_connector: bool):
    """Turns a list of graph nodes (a path) into RouteSegments, resolving
    which specific parallel road was used at each step (a MultiGraph can
    have more than one road between the same two intersections).

    `is_connector=True` forces every segment to be flagged as a connector
    (used for the pin-to-network link). `is_connector=False` instead flags
    each segment individually based on its own scenic_eligible value -- used
    for the fallback path, where MOST segments are real scenic road but a
    few might be a forced residential bridge."""
    segments = []
    for u, v in zip(path, path[1:]):
        # Reusing an edge can be necessary for a valid loop or a dead-end
        # connector. Suppressing it would make GeoJSON and distance disagree
        # with the path actually selected by NetworkX.
        parallel_edges = graph[u][v]
        best_key = min(parallel_edges, key=lambda k: parallel_edges[k][weight_key])
        edge_data = parallel_edges[best_key]
        seg = edge_data["segment"]
        if edge_data.get("reversed", False):
            seg = _reversed_segment(seg)
        flag = True if is_connector else (not seg.scenic_eligible)
        if flag:
            # Don't mutate the shared segment object (it may also appear via
            # a different graph view) -- copy it with the flag set.
            seg = replace(seg, is_connector=True)
        segments.append(seg)
    return segments


def _path_with_fallback(network: RoadNetwork, a, b):
    """Tries the scenic-only graph first (the common, preferred case).
    Only if that graph is genuinely disconnected between these two points
    does it fall back to the full graph (residential roads heavily
    penalized, not banned) -- see FALLBACK_PENALTY. Returns (path, graph,
    used_fallback) or raises nx.NetworkXNoPath if truly no route exists
    even allowing residential streets.
    """
    try:
        fun_path = nx.shortest_path(network.scenic, a, b, weight="cost")
        fun_length = sum(
            min(edges.values(), key=lambda edge: edge["cost"])["length_m"]
            for u, v in zip(fun_path, fun_path[1:])
            for edges in [network.scenic[u][v]]
        )
        a_data, b_data = network.scenic.nodes[a], network.scenic.nodes[b]
        direct_distance = math.hypot(
            (b_data["lon"] - a_data["lon"]) * 111_320.0
            * math.cos(math.radians((a_data["lat"] + b_data["lat"]) / 2)),
            (b_data["lat"] - a_data["lat"]) * 111_320.0,
        )
        # Only pay for a second path search when the scenic path is already
        # suspicious relative to the geometric lower bound.
        stretch_limit = MAX_LEG_STRETCH + MAX_PREFERENCE_STRETCH_BONUS * max(
            network.weights.get("curviness", 0.0),
            network.weights.get("scenery", 0.0),
            network.weights.get("landmarks", 0.0),
        )
        if direct_distance and fun_length > direct_distance * stretch_limit:
            short_path = nx.shortest_path(network.scenic, a, b, weight="length_m")
            short_length = sum(
                min(edges.values(), key=lambda edge: edge["length_m"])["length_m"]
                for u, v in zip(short_path, short_path[1:])
                for edges in [network.scenic[u][v]]
            )
            if fun_length > short_length * stretch_limit:
                logger.info(
                    "Capped scenic detour from %.0fm to %.0fm for leg %s -> %s.",
                    fun_length, short_length, a, b,
                )
                return short_path, network.scenic, False
        return fun_path, network.scenic, False
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        logger.warning(
            "No scenic-only path between %s and %s -- falling back to a "
            "penalized search that allows residential roads as a last resort.",
            a, b,
        )
        path = nx.shortest_path(network.full, a, b, weight="cost")
        return path, network.full, True


def _connect_to_scenic_network(
    network: RoadNetwork, pin_lon: float, pin_lat: float, *, from_scenic: bool = False,
    candidates: set | None = None,
):
    """Finds the shortest real path (via the FULL graph, residential roads
    allowed) from an arbitrary pin to the nearest point that exists on the
    scenic (non-residential) network. Returns (entry_node, connector_segments)
    or (None, []) if the pin isn't near any loaded road at all.
    """
    start_node = _nearest_node(network.full, pin_lon, pin_lat)
    if start_node is None:
        return None, []

    scenic_nodes = candidates if candidates is not None else set(network.scenic.nodes)
    if not scenic_nodes:
        return None, []

    if start_node in scenic_nodes:
        return start_node, []  # already directly on the scenic network

    try:
        # For an origin we need pin -> scenic, so search scenic -> pin on a
        # reversed view. For a destination we need scenic -> pin directly.
        search_graph = network.full if from_scenic else network.full.reverse(copy=False)
        _dist, path = nx.multi_source_dijkstra(
            search_graph, sources=scenic_nodes, target=start_node, weight="length_m"
        )
    except nx.NetworkXNoPath:
        return None, []

    # The origin search ran on a reversed view, so reverse its node order to
    # recover a valid path in the real graph. Destination order is ready.
    if not from_scenic:
        path = list(reversed(path))
    connector_segments = _edges_to_segments(network.full, path, "length_m", set(), is_connector=True)
    entry_node = path[0] if from_scenic else path[-1]
    return entry_node, connector_segments


def _round_trip_scenic_nodes(graph, minimum_component_size: int = 25) -> set:
    """Nodes in substantial directed components that can support a real loop."""
    qualifying = set()
    largest = set()
    for component in nx.strongly_connected_components(graph):
        if len(component) > len(largest):
            largest = set(component)
        if len(component) >= minimum_component_size:
            qualifying.update(component)
    return qualifying or largest


def _connect_loop_start(network: RoadNetwork, pin_lon: float, pin_lat: float):
    """Find legal outbound and return connectors to the loop-capable network."""
    round_trip_nodes = _round_trip_scenic_nodes(network.scenic)
    if not round_trip_nodes:
        return None, [], None, [], set()
    entry, connector_out = _connect_to_scenic_network(
        network, pin_lon, pin_lat, candidates=round_trip_nodes,
    )
    exit_node, connector_back = _connect_to_scenic_network(
        network, pin_lon, pin_lat, from_scenic=True, candidates=round_trip_nodes,
    )
    return entry, connector_out, exit_node, connector_back, round_trip_nodes


def _pick_waypoints(
    graph, anchor_node, target_distance_m: float, weights: dict,
    candidates: set | None = None, n_waypoints: int = 4,
):
    """Picks rough anchor points forming a loop shape around the anchor, at
    about target_distance / n_waypoints from each other. These are just
    geometric targets -- the actual road selection happens in the weighted
    shortest-path search between them, not here. Operates on the SCENIC
    graph only, so a loop's shape is never anchored on a residential node."""
    anchor_lon = graph.nodes[anchor_node]["lon"]
    anchor_lat = graph.nodes[anchor_node]["lat"]
    leg_distance_m = target_distance_m / n_waypoints
    meters_per_deg_lat = 111_320.0
    meters_per_deg_lon = 111_320.0 * math.cos(math.radians(anchor_lat))

    waypoint_candidates = _safe_waypoint_nodes(graph, candidates)
    waypoints = []
    base_angle = random.uniform(0, 2 * math.pi)
    for i in range(1, n_waypoints):
        angle = base_angle + (2 * math.pi * i / n_waypoints)
        radius_m = leg_distance_m * n_waypoints / (2 * math.pi) * 1.1
        target_lon = anchor_lon + (radius_m * math.cos(angle)) / meters_per_deg_lon
        target_lat = anchor_lat + (radius_m * math.sin(angle)) / meters_per_deg_lat
        waypoints.append(_nearest_preferred_node(
            graph, target_lon, target_lat, candidates=waypoint_candidates,
            weights=weights, preference_reach_m=max(1800.0, radius_m * 0.55),
        ))
    return waypoints


def generate_loop(
    network: RoadNetwork, start_lon: float, start_lat: float, target_distance_m: float,
):
    entry_node, connector_out, exit_node, connector_back, round_trip_nodes = (
        _connect_loop_start(network, start_lon, start_lat)
    )
    if entry_node is None or exit_node is None:
        return None

    waypoints = _pick_waypoints(
        network.scenic, entry_node, target_distance_m, network.weights,
        candidates=round_trip_nodes,
    )
    logger.info("Freeform loop selected %d distinct waypoint anchors.", len(set(waypoints)))
    if any(waypoint is None for waypoint in waypoints):
        return None
    stops = [entry_node] + waypoints + [exit_node]

    route = GeneratedRoute()
    route_body = []
    seen_edges: set = set()

    for seg in connector_out:
        route.segments.append(seg)
        route.total_length_m += seg.length_m

    for a, b in zip(stops, stops[1:]):
        if a == b:
            continue
        try:
            path, graph_used, _used_fallback = _path_with_fallback(network, a, b)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            logger.warning("No complete loop path between %s and %s.", a, b)
            return None
        route_body.extend(
            _edges_to_segments(graph_used, path, "cost", seen_edges, is_connector=False)
        )

    cleaned_body = _remove_immediate_retracing(route_body)
    logger.info(
        "Freeform retrace cleanup retained %d of %d route-body segments.",
        len(cleaned_body), len(route_body),
    )
    if not cleaned_body:
        return None
    for seg in cleaned_body:
        route.segments.append(seg)
        route.total_length_m += seg.length_m

    # The return connector is searched independently so one-way restrictions
    # remain legal; it may differ from the outbound neighbourhood path.
    for seg in connector_back:
        route.segments.append(seg)
        route.total_length_m += seg.length_m

    if not route.segments:
        return None
    return route


def generate_exploration_loop(
    network: RoadNetwork,
    start_lon: float,
    start_lat: float,
    focus_lon: float,
    focus_lat: float,
    target_distance_m: float,
):
    """Build a home-returning loop biased around a user-selected area.

    The focus is an exploration anchor, not a stop or destination: the route
    reaches its nearby scenic network, samples roads around it, then returns
    to the original start.
    """
    entry_node, connector_out, exit_node, connector_back, round_trip_nodes = (
        _connect_loop_start(network, start_lon, start_lat)
    )
    if entry_node is None or exit_node is None:
        return None

    # Exploration anchors must all live in the same round-trip-capable road
    # component as the start. This matters near state/extract boundaries:
    # nearest-node snapping can otherwise select a disconnected road fragment
    # on the far side of the boundary and make every request fail.
    routable_full_nodes = None
    for component in nx.strongly_connected_components(network.full):
        if entry_node in component:
            routable_full_nodes = component
            break
    if not routable_full_nodes:
        return None
    routable_scenic_nodes = (
        set(network.scenic.nodes).intersection(routable_full_nodes).intersection(round_trip_nodes)
    )
    if not routable_scenic_nodes:
        return None
    waypoint_candidates = _safe_waypoint_nodes(network.scenic, routable_scenic_nodes)

    focus_node = _nearest_preferred_node(
        network.scenic, focus_lon, focus_lat, candidates=waypoint_candidates,
        weights=network.weights, preference_reach_m=2200.0,
    )
    if focus_node is None:
        return None

    entry_lon, entry_lat = network.scenic.nodes[entry_node]["lon"], network.scenic.nodes[entry_node]["lat"]
    focus_node_lon = network.scenic.nodes[focus_node]["lon"]
    focus_node_lat = network.scenic.nodes[focus_node]["lat"]
    mean_lat = math.radians((entry_lat + focus_node_lat) / 2)
    direct_m = math.hypot(
        (focus_node_lon - entry_lon) * 111_320.0 * math.cos(mean_lat),
        (focus_node_lat - entry_lat) * 111_320.0,
    )
    # Reserve the unavoidable out-and-back distance, then use the remainder
    # for a small circuit around the selected exploration area.
    local_distance_m = max(target_distance_m - 2 * direct_m, target_distance_m * 0.2)
    # Aim the local circuit beyond the focus relative to home. This prevents
    # a focus east of the city from randomly placing its exploration anchors
    # back across the city to the west.
    away_angle = math.atan2(
        (focus_node_lat - entry_lat) * 111_320.0,
        (focus_node_lon - entry_lon) * 111_320.0 * math.cos(mean_lat),
    )
    local_radius_m = max(1200.0, local_distance_m / (2 * math.pi))
    meters_per_deg_lon = 111_320.0 * math.cos(math.radians(focus_node_lat))
    # Try arcs facing away, left, right, and back toward home. Select the arc
    # whose desired coordinates require the least snapping to the connected
    # loaded graph. Near Minnesota's eastern boundary this automatically
    # chooses a Minnesota-side circuit instead of unreachable Wisconsin.
    best_waypoints, best_snap_error = None, float("inf")
    for rotation in (0, math.pi / 2, -math.pi / 2, math.pi):
        candidate_waypoints = []
        snap_error = 0.0
        for offset in (-math.pi / 3, 0, math.pi / 3):
            angle = away_angle + rotation + offset
            target_lon = focus_node_lon + local_radius_m * math.cos(angle) / meters_per_deg_lon
            target_lat = focus_node_lat + local_radius_m * math.sin(angle) / 111_320.0
            node = _nearest_preferred_node(
                network.scenic, target_lon, target_lat, candidates=waypoint_candidates,
                weights=network.weights,
                preference_reach_m=max(1800.0, local_radius_m * 0.5),
            )
            if node is None:
                snap_error = float("inf")
                break
            node_data = network.scenic.nodes[node]
            snap_error += math.hypot(
                (node_data["lon"] - target_lon) * meters_per_deg_lon,
                (node_data["lat"] - target_lat) * 111_320.0,
            )
            nearby_edges = list(network.scenic.out_edges(node, data=True))
            if nearby_edges:
                local_urban_density = sum(edge[2].get("urban_density", 0) for edge in nearby_edges) / len(nearby_edges)
                local_scenery = max(
                    (edge[2]["segment"].scenery_score or 0) / 100.0
                    for edge in nearby_edges
                )
                snap_error += (
                    local_radius_m * 2.0 * network.weights.get("city_avoidance", 0.8)
                    * local_urban_density
                )
                snap_error -= (
                    local_radius_m * 0.7 * network.weights.get("scenery", 0.0)
                    * local_scenery
                )
                local_landmark = max(
                    edge[2].get("landmark_score", 0.0) for edge in nearby_edges
                )
                snap_error -= (
                    local_radius_m * 0.9 * network.weights.get("landmarks", 0.0)
                    * local_landmark
                )
            candidate_waypoints.append(node)
        # Avoid a collapsed "circuit" where multiple desired anchors snap to
        # the same boundary node.
        if len(set(candidate_waypoints)) < 2:
            snap_error += local_radius_m * 10
        if snap_error < best_snap_error:
            best_waypoints, best_snap_error = candidate_waypoints, snap_error
    local_waypoints = best_waypoints or []
    if any(waypoint is None for waypoint in local_waypoints):
        return None

    stops = [entry_node, focus_node, *local_waypoints, exit_node]
    route = GeneratedRoute()
    route_body = []
    for seg in connector_out:
        route.segments.append(seg)
        route.total_length_m += seg.length_m

    for a, b in zip(stops, stops[1:]):
        if a == b:
            continue
        try:
            path, graph_used, _used_fallback = _path_with_fallback(network, a, b)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            logger.warning("No complete exploration-loop path between %s and %s.", a, b)
            return None
        route_body.extend(
            _edges_to_segments(graph_used, path, "cost", set(), is_connector=False)
        )

    cleaned_body = _remove_immediate_retracing(route_body)
    if not cleaned_body:
        return None
    for seg in cleaned_body:
        route.segments.append(seg)
        route.total_length_m += seg.length_m

    for seg in connector_back:
        route.segments.append(seg)
        route.total_length_m += seg.length_m

    return route if route.segments else None


def generate_point_to_point(
    network: RoadNetwork,
    start_lon: float, start_lat: float,
    dest_lon: float, dest_lat: float,
):
    """Finds the most 'fun' route (per the network's cost weighting) between
    two fixed points, with residential connectors only at the very start and
    very end."""
    entry_node, connector_in = _connect_to_scenic_network(network, start_lon, start_lat)
    if entry_node is None:
        return None
    exit_node, connector_out = _connect_to_scenic_network(
        network, dest_lon, dest_lat, from_scenic=True
    )
    if exit_node is None:
        return None

    route = GeneratedRoute()
    for seg in connector_in:
        route.segments.append(seg)
        route.total_length_m += seg.length_m

    try:
        path, graph_used, _used_fallback = _path_with_fallback(network, entry_node, exit_node)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None

    for seg in _edges_to_segments(graph_used, path, "cost", set(), is_connector=False):
        route.segments.append(seg)
        route.total_length_m += seg.length_m

    for seg in connector_out:
        route.segments.append(seg)
        route.total_length_m += seg.length_m

    if not route.segments:
        return None
    return route
