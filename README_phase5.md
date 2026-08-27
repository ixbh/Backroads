# Phase 5 — Route Generation (this is "the app")

Three files, updated based on real feedback from testing this:

- `routing.py` — builds a road network graph and finds a route. **Rewritten**
  to hard-exclude residential/living_street streets from the actual
  recommended route -- they can now only appear as a short, clearly-flagged
  "connector" segment linking your literal start/destination pin to the real
  road network (because most starting points are, in reality, a driveway).
- `api.py` — the HTTP API. **Now speaks miles, not km**, and supports an
  optional destination for point-to-point routing (omit it for a loop).
- `viewer.html` — **now click-to-pin** instead of typing coordinates: click
  the map for a start pin, click again for an optional destination pin.

## What changed and why (the four things you flagged)

**1. Miles, not km.** Every distance in the API and viewer is now in miles.
`target_distance_mi`, `search_radius_mi`, `length_mi` throughout.

**2. Click-to-pin start + destination.** Click the map once for a start pin
(green marker). Click again for an optional destination pin (red marker) --
if you set one, you get a point-to-point "most scenic route from A to B"
instead of a loop. A third click resets and starts over. No more typing
latitude/longitude by hand.

**3. (this is the important one) Residential streets are now hard-excluded,
not just penalized.** The old version made residential roads *more
expensive* to route through, which wasn't strong enough -- with enough
detour savings, the pathfinder could still decide cutting through a
neighborhood was "worth it." That's what you actually hit. The new version
builds two separate road networks:
  - a "full" network (everything, including residential) used *only* to
    find the shortest possible link from your exact pin to the real road
    network, and
  - a "scenic" network (residential/living_street removed entirely) used
    for the actual route -- the loop shape, the waypoint selection, all of
    it.

  This was tested against a deliberately-built scenario: a 3x3 residential
  subdivision with exactly one exit, with the start pin placed at the far
  corner (like a real driveway). Result: every residential segment in the
  output was correctly flagged as a connector, appearing only at the very
  start (leaving the subdivision) and very end (returning to it) -- the
  actual loop body used zero residential streets. `is_connector: true` is
  in the API response and GeoJSON properties for every segment, so your
  frontend can render these differently (the viewer draws them dashed gray).

## How the routing works (updated)

1. Find the nearest point on the real (non-residential) road network to
   your pin -- this is the connector, and it's the only place a residential
   street can appear.
2. From there, either:
   - **Loop mode** (no destination set): pick a few rough waypoints forming
     a loop shape at about the right radius for your target distance, then
     weighted-shortest-path between them and back.
   - **Point-to-point mode** (destination set): one weighted-shortest-path
     straight from your entry point to the nearest network point to your
     destination, then a connector out to the exact destination pin.
3. Either way, each road's "cost" is discounted based on how well it matches
   your curviness/traffic preferences, so the pathfinder naturally prefers
   fun roads over boring ones without needing a full custom solver.

This was re-validated after the rewrite: on the same test grid as before,
curviness-weighted loops averaged a 52.6 curviness score vs. 45.3 for
unweighted, averaged across 20 independent trials (not just one lucky
comparison this time).

## 1. Install dependencies (unchanged)

```powershell
pip install networkx fastapi "uvicorn[standard]"
```

## 2. Run the API (unchanged)

```powershell
uvicorn api:app --reload --port 8000
```

## 3. Use the viewer

Open `viewer.html` in Firefox. Click the map for a start pin. Optionally
click again for a destination pin. Set your preferences, hit **Generate
Route**. Dashed gray = unavoidable connector through a neighborhood; solid,
colored by curviness (gray=straight, red=curvy) = the actual recommended
route.

## Known limitations, still honest

- **Scenery is OSM proximity-based** -- water, forest, protected land, countryside, natural land, and viewpoints now affect routing after Phase 4 preprocessing. It does not yet measure visibility, elevation, or photographic quality.
- **Target distance is still approximate**, loop mode only.
- **The residential-avoidance fix hasn't been tested against your real
  300K-road Minnesota data yet** -- only against a purpose-built synthetic
  neighborhood. Real OSM data has messier edge cases (e.g. a residential
  street that's the ONLY way in/out of a whole rural area) -- if you hit a
  spot where no route can be found at all, that's the likely reason, and
  worth telling me about with the specific location.
- **A "third click resets" pin system is a simple first pass** -- no way
  yet to drag a pin to adjust it, or to see the pin's address/name.
