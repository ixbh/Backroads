"""
Phase 5 backend: exposes route generation as an HTTP API a frontend can
actually call.

Run it with:
    uvicorn api:app --reload --port 8000

POST to http://localhost:8000/generate-route -- see RouteRequest below.
If `dest_lat`/`dest_lon` are omitted, generates a loop back to the start.
If they're provided, generates a point-to-point route instead.

All user-facing distances are in MILES (not km) -- this app is for American
drivers. Internally everything still runs in meters.
"""

from __future__ import annotations

import gc
import os
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from road_store import RoadStoreConfigurationError, open_road_source
from routing import (
    build_network,
    generate_exploration_loop,
    generate_loop,
    generate_point_to_point,
)

# Loads DATABASE_URL (and anything else) from a .env file in this folder, so
# you don't need to re-run $env:DATABASE_URL="..." every time you open a new
# terminal -- that's what caused the 500 error you just hit.
load_dotenv()

MILES_TO_METERS = 1609.344
NETWORK_CACHE_TTL_SECONDS = 300
_network_cache: tuple[tuple, float, object] | None = None
_network_cache_lock = threading.Lock()
_route_generation_lock = threading.Lock()

app = FastAPI(title="Scenic Route Generator API")

configured_origins = os.environ.get("ALLOWED_ORIGINS")
allowed_origins = (
    [origin.strip() for origin in configured_origins.split(",") if origin.strip()]
    if configured_origins else ["*"]
)
app.add_middleware(
    CORSMiddleware,
    # "*" is fine for local development with no cookies/credentials involved.
    # Tighten this to your real frontend's origin before deploying anywhere public.
    allow_origins=allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_connection():
    try:
        return open_road_source()
    except RoadStoreConfigurationError as error:
        raise HTTPException(500, str(error)) from error


def _get_network(region, center_lon, center_lat, radius_m, weights):
    """Cache the most recent immutable graph; graph construction dominates latency."""
    global _network_cache
    key = (
        region, round(center_lon, 5), round(center_lat, 5), round(radius_m, -1),
        round(weights["curviness"], 2), round(weights["traffic"], 2),
        round(weights["city_avoidance"], 2), round(weights["scenery"], 2),
        bool(weights["paved_only"]),
    )
    now = time.monotonic()
    with _network_cache_lock:
        if _network_cache and _network_cache[0] == key and now - _network_cache[1] < NETWORK_CACHE_TTL_SECONDS:
            return _network_cache[2]

        # A preference or search-area change needs a differently weighted
        # graph. Drop the previous graph *before* constructing its replacement
        # so a 512 MB hosted instance never holds two metro-sized NetworkX
        # graphs at once. Completed requests own no other reference because
        # route generation is serialized below.
        _network_cache = None
        gc.collect()

        conn = get_connection()
        try:
            network = build_network(conn, region, center_lon, center_lat, radius_m, weights)
        finally:
            conn.close()
        # A single-entry cache bounds memory: real metro graphs can contain
        # hundreds of thousands of nodes and should not accumulate.
        _network_cache = (key, time.monotonic(), network)
        return network


class RouteRequest(BaseModel):
    region: str = Field(..., examples=["minnesota"])
    start_lat: float = Field(ge=-90, le=90)
    start_lon: float = Field(ge=-180, le=180)
    dest_lat: float | None = Field(
        default=None, ge=-90, le=90,
        description="If set (with dest_lon), generates a point-to-point "
                    "route instead of a loop back to the start."
    )
    dest_lon: float | None = Field(default=None, ge=-180, le=180)
    focus_lat: float | None = Field(
        default=None, ge=-90, le=90,
        description="Exploration anchor. If set with focus_lon, generate a loop "
                    "through roads near this area and return to the start.",
    )
    focus_lon: float | None = Field(default=None, ge=-180, le=180)
    target_distance_mi: float = Field(
        default=20, gt=0, le=300,
        description="Used to size the loop and the search radius. For "
                    "point-to-point routes this mainly affects search radius, "
                    "not the actual route length (that's determined by the "
                    "start/destination pair itself).",
    )
    curviness_weight: float = Field(default=0.5, ge=0, le=1)
    traffic_weight: float = Field(default=0.5, ge=0, le=1)
    city_avoidance: float = Field(
        default=0.8, ge=0, le=1,
        description="Strength of the penalty derived from nearby OSM stop/signal density.",
    )
    scenery_weight: float = Field(default=0.5, ge=0, le=1)
    paved_only: bool = Field(
        default=True,
        description="Exclude roads explicitly tagged unpaved and require tracks to have an explicit paved surface.",
    )
    search_radius_mi: float | None = Field(
        default=None,
        description="How far around the start to load candidate roads. "
                    "Defaults to a bit more than target_distance_mi / 2 for "
                    "loops, or the start-destination distance for point-to-point.",
    )


class RouteSegmentOut(BaseModel):
    road_id: int
    name: str | None
    highway: str
    surface: str | None
    tracktype: str | None
    length_mi: float
    curviness_score: int | None
    scenery_score: int | None
    scenery_signals: dict
    scenic_eligible: bool
    is_connector: bool


class RouteResponse(BaseModel):
    route_kind: str
    total_length_mi: float
    estimated_time_min: int
    route_score: int | None
    curviness_score: int | None
    scenery_score: int | None
    scenery_signals: list[str]
    score_basis: str
    segment_count: int
    connector_length_mi: float
    known_paved_length_mi: float
    unknown_surface_length_mi: float
    known_unpaved_length_mi: float
    surface_notice: str
    segments: list[RouteSegmentOut]
    geojson: dict


def _haversine_mi(lat1, lon1, lat2, lon2) -> float:
    import math
    r_mi = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * r_mi * math.asin(math.sqrt(a))


DEFAULT_SPEED_MPH = {
    "motorway": 65, "motorway_link": 35, "trunk": 55, "trunk_link": 35,
    "primary": 50, "primary_link": 30, "secondary": 45,
    "secondary_link": 30, "tertiary": 40, "tertiary_link": 25,
    "unclassified": 35, "residential": 25, "living_street": 10, "track": 15,
}


def _route_summary(route, curviness_weight: float, scenery_weight: float):
    """Return ETA plus length-weighted, explainable route signals."""
    hours = sum(
        (segment.length_m / MILES_TO_METERS) / DEFAULT_SPEED_MPH.get(segment.highway, 30)
        for segment in route.segments
    )
    curved = [
        segment for segment in route.segments
        if not segment.is_connector and segment.curviness_score is not None
    ]
    scenic = [
        segment for segment in route.segments
        if not segment.is_connector and segment.scenery_score is not None
    ]
    curved_length = sum(segment.length_m for segment in curved)
    scenic_length = sum(segment.length_m for segment in scenic)
    curve_score = (
        round(sum(s.curviness_score * s.length_m for s in curved) / curved_length)
        if curved_length else None
    )
    scenery_score = (
        round(sum(s.scenery_score * s.length_m for s in scenic) / scenic_length)
        if scenic_length else None
    )
    weighted = []
    if curve_score is not None and curviness_weight > 0:
        weighted.append((curve_score, curviness_weight))
    if scenery_score is not None and scenery_weight > 0:
        weighted.append((scenery_score, scenery_weight))
    route_score = round(sum(score * weight for score, weight in weighted) / sum(weight for _, weight in weighted)) if weighted else None
    signal_lengths = {}
    for segment in scenic:
        for signal, present in segment.scenery_signals.items():
            if present:
                signal_lengths[signal] = signal_lengths.get(signal, 0) + segment.length_m
    notable_signals = [
        signal for signal, _ in sorted(signal_lengths.items(), key=lambda item: item[1], reverse=True)
    ]
    return max(1, round(hours * 60)), route_score, curve_score, scenery_score, notable_signals


def _surface_summary(route):
    from routing import KNOWN_PAVED_SURFACES, KNOWN_UNPAVED_SURFACES

    totals = {"paved": 0.0, "unpaved": 0.0, "unknown": 0.0}
    for segment in route.segments:
        surface = (segment.surface or "").strip().lower()
        if surface in KNOWN_PAVED_SURFACES:
            totals["paved"] += segment.length_m
        elif surface in KNOWN_UNPAVED_SURFACES or segment.highway == "track":
            totals["unpaved"] += segment.length_m
        else:
            totals["unknown"] += segment.length_m
    return totals


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def viewer():
    """Serve the map and API from one HTTPS origin in production."""
    return FileResponse(Path(__file__).with_name("viewer.html"))


@app.get("/manifest.webmanifest", include_in_schema=False)
def web_manifest():
    return FileResponse(
        Path(__file__).with_name("manifest.webmanifest"),
        media_type="application/manifest+json",
    )


def _generate_route(req: RouteRequest):
    if (req.dest_lat is None) != (req.dest_lon is None):
        raise HTTPException(422, "dest_lat and dest_lon must be provided together.")
    if (req.focus_lat is None) != (req.focus_lon is None):
        raise HTTPException(422, "focus_lat and focus_lon must be provided together.")
    if req.dest_lat is not None and req.focus_lat is not None:
        raise HTTPException(422, "Choose either a destination or an exploration area, not both.")
    is_point_to_point = req.dest_lat is not None and req.dest_lon is not None
    is_exploration_loop = req.focus_lat is not None and req.focus_lon is not None

    if is_point_to_point or is_exploration_loop:
        # BUG (fixed): this used to center the search on start_lon/start_lat
        # only, with a radius sized to reach the destination -- but centering
        # on the START and sizing the RADIUS for the whole trip means a
        # destination a full radius away sits right at the edge, and often
        # outside it once rounding is considered. The search has to center on
        # the MIDPOINT between start and destination, not on the start.
        target_lon = req.dest_lon if is_point_to_point else req.focus_lon
        target_lat = req.dest_lat if is_point_to_point else req.focus_lat
        center_lon = (req.start_lon + target_lon) / 2
        center_lat = (req.start_lat + target_lat) / 2
        trip_mi = _haversine_mi(req.start_lat, req.start_lon, target_lat, target_lon)
        if is_exploration_loop and trip_mi * 2 > req.target_distance_mi * 1.25:
            minimum_mi = trip_mi * 2
            raise HTTPException(
                422,
                f"The exploration area is {trip_mi:.1f}mi from the start, so the round trip "
                f"needs at least about {minimum_mi:.0f}mi. Increase the target distance or "
                "choose a closer exploration area.",
            )
        # Half the straight-line trip reaches both endpoints exactly; a 40%
        # buffer on top gives the router room to actually detour for fun
        # roads instead of tracing the straight line, plus a flat +3mi so
        # very short trips still get a reasonable amount of choice.
        if is_exploration_loop:
            # Cover the home-focus corridor plus the actual local circuit,
            # rather than loading target_distance/2 in every direction.
            local_mi = max(req.target_distance_mi - 2 * trip_mi, req.target_distance_mi * 0.2)
            local_radius_mi = max(1.0, local_mi / (2 * 3.141592653589793))
            default_radius_mi = trip_mi / 2 + local_radius_mi * 1.15 + 2
        else:
            default_radius_mi = trip_mi / 2 * 1.4 + 3
        radius_mi = req.search_radius_mi or default_radius_mi
    else:
        center_lon, center_lat = req.start_lon, req.start_lat
        # A roughly circular loop has radius circumference/(2*pi); retain a
        # generous network-detour buffer without loading a half-distance disk.
        radius_mi = req.search_radius_mi or (req.target_distance_mi / (2 * 3.141592653589793) * 1.8 + 3)

    radius_m = radius_mi * MILES_TO_METERS
    target_distance_m = req.target_distance_mi * MILES_TO_METERS

    network = _get_network(
        req.region, center_lon, center_lat, radius_m,
        weights={
            "curviness": req.curviness_weight,
            "traffic": req.traffic_weight,
            "city_avoidance": req.city_avoidance,
            "scenery": req.scenery_weight,
            "paved_only": req.paved_only,
        },
    )

    if network.full.number_of_nodes() == 0:
        raise HTTPException(
            404,
            f"No roads found within {radius_mi:.1f}mi of the search center "
            f"in region '{req.region}'. Check the region name and coordinates.",
        )

    if is_point_to_point:
        route = generate_point_to_point(network, req.start_lon, req.start_lat, req.dest_lon, req.dest_lat)
        route_kind = "point_to_point"
    elif is_exploration_loop:
        route = generate_exploration_loop(
            network, req.start_lon, req.start_lat,
            req.focus_lon, req.focus_lat, target_distance_m,
        )
        # The first circuit is geometry-sized, but real road topology can
        # shorten or lengthen it. Make one cheap refinement on the already
        # cached graph when it misses the request by more than 15%.
        if route and route.total_length_m > 0:
            distance_ratio = target_distance_m / route.total_length_m
            if distance_ratio < 0.9 or distance_ratio > 1.1:
                adjusted_target_m = target_distance_m * max(0.7, min(1.3, distance_ratio))
                refined_route = generate_exploration_loop(
                    network, req.start_lon, req.start_lat,
                    req.focus_lon, req.focus_lat, adjusted_target_m,
                )
                if refined_route and abs(refined_route.total_length_m - target_distance_m) < abs(route.total_length_m - target_distance_m):
                    route = refined_route
        route_kind = "exploration_loop"
    else:
        route = generate_loop(network, req.start_lon, req.start_lat, target_distance_m)
        route_kind = "loop"

    if route is None:
        raise HTTPException(
            422,
            "Could not build a route with these settings -- try a larger "
            "search radius, or check that the start/destination are near "
            "roads that exist in this region's data.",
        )

    connector_length_m = sum(s.length_m for s in route.segments if s.is_connector)
    estimated_time_min, route_score, curve_score, scenery_score, scenery_signals = _route_summary(
        route, req.curviness_weight, req.scenery_weight
    )
    surface_totals = _surface_summary(route)
    unknown_surface_mi = surface_totals["unknown"] / MILES_TO_METERS
    known_unpaved_mi = surface_totals["unpaved"] / MILES_TO_METERS
    if req.paved_only and unknown_surface_mi > 0:
        surface_notice = (
            "Known dirt, gravel, compacted, and unpaved roads were excluded. "
            f"OSM has no surface tag for {unknown_surface_mi:.1f} mi, so pavement cannot be guaranteed there."
        )
    elif req.paved_only:
        surface_notice = "Every route segment has an OSM surface tag recognized as paved."
    else:
        surface_notice = f"Paved-only is off; the route includes {known_unpaved_mi:.1f} mi tagged as unpaved or track."

    return RouteResponse(
        route_kind=route_kind,
        total_length_mi=round(route.total_length_m / MILES_TO_METERS, 2),
        estimated_time_min=estimated_time_min,
        route_score=route_score,
        curviness_score=curve_score,
        scenery_score=scenery_score,
        scenery_signals=scenery_signals,
        score_basis=(
            "Route score combines OSM geometry curviness with nearby OSM landscape features. "
            "Dense-area avoidance uses stop/signal and road-network density; live traffic is not scored."
        ),
        connector_length_mi=round(connector_length_m / MILES_TO_METERS, 2),
        known_paved_length_mi=round(surface_totals["paved"] / MILES_TO_METERS, 2),
        unknown_surface_length_mi=round(unknown_surface_mi, 2),
        known_unpaved_length_mi=round(known_unpaved_mi, 2),
        surface_notice=surface_notice,
        segment_count=len(route.segments),
        segments=[
            RouteSegmentOut(
                road_id=s.road_id, name=s.name, highway=s.highway,
                surface=s.surface, tracktype=s.tracktype,
                length_mi=round(s.length_m / MILES_TO_METERS, 2),
                curviness_score=s.curviness_score,
                scenery_score=s.scenery_score,
                scenery_signals=s.scenery_signals,
                scenic_eligible=s.scenic_eligible,
                is_connector=s.is_connector,
            )
            for s in route.segments
        ],
        geojson=route.to_geojson(),
    )


@app.post("/generate-route", response_model=RouteResponse)
def generate_route(req: RouteRequest):
    # Sync FastAPI handlers can otherwise run concurrently in its threadpool.
    # A browser refresh does not cancel Python graph work already underway, so
    # overlapping builds can exceed Render Free's 512 MB memory limit. Reject
    # the overlap with an explainable retry response instead of crashing the
    # whole service and losing the in-memory cache.
    if not _route_generation_lock.acquire(blocking=False):
        raise HTTPException(
            429,
            "Another route is still being generated. Wait for it to finish, then try again.",
        )
    try:
        return _generate_route(req)
    finally:
        _route_generation_lock.release()
