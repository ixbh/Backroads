# Scenic Route Generator

An early local-first route-planning prototype built from OpenStreetMap data. It supports point-to-point drives, freeform loops, and exploration loops that bias the drive around a selected area before returning to the start. Curvature, scenery, and dense-area avoidance use explainable OSM-derived heuristics. Elevation, live traffic, stops, saving, sharing, export, and navigation are not implemented yet.

## Architecture

- `load_osm.py`: imports a regional OSM extract into PostGIS, rejecting private/inaccessible and unsuitable road classes.
- `curviness.py`: computes geometry-based curvature and intersection-conflict signals, then normalizes curvature to a regional 0–100 percentile.
- `load_scenic_features.py` and `scenery.py`: import explicit OSM landscape features and assign explainable water, forest, protected-land, countryside, natural-land, and viewpoint scores.
- `routing.py`: builds directed NetworkX graphs from exact OSM node identities, preserves forward and reverse one-way restrictions, filters known unpaved surfaces, keeps residential roads out of the preferred graph, combines stop/signal density with local road-network density for city avoidance, and uses residential streets only as penalized/connector fallbacks.
- `api.py`: FastAPI adapter for route generation and response summaries, with a bounded five-minute cache for the most recent expensive graph build.
- `viewer.html`: MapLibre/OpenFreeMap click-to-pin client.

The route score combines length-weighted curviness with OSM scenery evidence. Dense-area avoidance combines nearby OSM stop/signal density with a local road-network-density proxy; it is not census population or live traffic. ETA uses documented road-class speed assumptions. These values are not navigation-grade.

## Setup

1. Create a Python 3.11+ virtual environment and run `pip install -r requirements.txt`.
2. Copy `.env.example` to `.env` and set the server-side `DATABASE_URL`.
3. Apply `schema.sql`, import an OSM extract with `load_osm.py`, and run `curviness.py`. Existing databases created before the topology update should apply `migration_road_topology.sql` and re-run `load_osm.py` so exact OSM node IDs are populated.
4. Import and score landscape signals with `load_scenic_features.py --pbf minnesota-latest.osm.pbf --region minnesota --replace`, then `scenery.py --region minnesota`.
5. Start the API with `uvicorn api:app --reload --port 8000`.
6. Open `http://localhost:8000/`; FastAPI serves the viewer and API together. Opening `viewer.html` directly still falls back to `http://localhost:8000` for local development.

See `DEPLOYMENT.md` for the hosted beta architecture and iPhone/navigation roadmap.

Opening the viewer as a plain `file://` URL may be restricted by browser security policies. The map style and tiles require internet access; routing itself uses the local database.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The focused tests use synthetic networks and do not require PostgreSQL.

## Environment and external services

- `DATABASE_URL` — required by import, scoring, and route-generation API calls; never expose it to the browser.
- PostgreSQL + PostGIS — required.
- OpenFreeMap — public basemap style/tiles used by `viewer.html`; no API key.
- Geofabrik — optional source for downloading regional OSM PBF extracts.

## Current product limitations

- Loop distance is approximate and only one candidate is returned.
- Exploration requests reject a target distance that is physically too short for the selected area's out-and-back distance; generated scenic legs are capped to prevent extreme preference-driven detours.
- Curviness, dense-area avoidance, and OSM scenery proximity are heuristics; elevation and visual-quality grading are not implemented.
- Paved-only mode excludes explicit dirt, gravel, compacted, unpaved surfaces and unknown tracks. Regular roads with no OSM surface tag remain usable but are disclosed, so pavement cannot yet be guaranteed for every mile.
- No toll, motorway, POI, or live traffic preferences yet.
- No persistent cache, saved routes, share/export, or turn-by-turn navigation.
- Exact OSM nodes prevent false bridge/underpass intersections, but OSM restrictions beyond `oneway` (notably turn restrictions) are not yet modeled, so routes remain exploratory rather than navigation-grade.
