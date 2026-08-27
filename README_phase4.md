# Phase 4 — Explainable OSM Scenery

Phase 4 adds real, inspectable landscape evidence without external API keys.

## Signals

- water: lakes, reservoirs, rivers, and canals;
- forest: OSM forest and woodland;
- park: nature reserves and protected areas (ordinary urban parks are excluded);
- countryside: farmland, meadow, orchard, and vineyard;
- natural: wetland, heath, grassland, cliff, ridge, and peak;
- viewpoint: explicitly tagged OSM viewpoints.

Minor streams and ordinary urban parks are intentionally excluded because testing showed they made scores too common and could reward city routes. Each road stores both a 0–100 score and the exact signal names that contributed. This is proximity-based evidence, not proof that scenery is visible from the road.

## Run

Apply `migration_scenery.sql` to an existing database, then:

```powershell
python load_scenic_features.py --pbf minnesota-latest.osm.pbf --region minnesota --replace
python scenery.py --region minnesota
```

Re-run both commands after replacing the OSM extract. The import and statewide spatial score are offline preprocessing steps; route requests read the cached scores.
