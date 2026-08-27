# Phase 2 — Candidate Road Collection

Loads a regional OpenStreetMap extract into your PostGIS database. No Google
API keys needed for this phase at all.

## 1. Install dependencies

```bash
pip install osmium psycopg2-binary
```

(If your FastAPI backend already has a virtualenv/poetry/uv setup, add these
there instead.)

## 2. Apply the schema

```bash
psql "$DATABASE_URL" -f schema.sql
```

## 3. Sanity-check with the included tiny test file first

Before pulling a multi-gigabyte state extract, confirm your local setup
actually works end-to-end against `sample.osm` (a hand-built 6-node, 3-way
fixture — one real road, one footway, one grade5 track — so you can confirm
the filtering logic behaves as expected):

```bash
export DATABASE_URL="postgresql://user:pass@localhost:5432/your_db"
python load_osm.py --pbf sample.osm --region test_region
```

Expected output: `1 roads and 1 intersections loaded` (the footway and the
grade5 track both get correctly filtered out).

Check it landed right:

```sql
SELECT osm_id, name, highway, surface, round(length_m::numeric,1) AS length_m
FROM roads WHERE region = 'test_region';
```

## 4. Download the real Minnesota extract

This has to happen on your machine — this sandbox can't reach
download.geofabrik.de.

```bash
curl -O https://download.geofabrik.de/north-america/us/minnesota-latest.osm.pbf
```

(~150-250MB, updated regularly by Geofabrik.)

## 5. Run the real ingestion

```bash
python load_osm.py --pbf minnesota-latest.osm.pbf --region minnesota
```

This will take a few minutes depending on your machine. Expect somewhere in
the range of a few hundred thousand candidate road segments for the whole
state — that's normal; Phase 3/4 scoring is what narrows this down to
"actually fun to drive."

## 6. Confirm

```sql
SELECT highway, count(*) FROM roads WHERE region = 'minnesota' GROUP BY highway ORDER BY count(*) DESC;
SELECT count(*) FROM intersections WHERE region = 'minnesota';
```

Once this looks right, delete the `test_region` rows and you're ready for
Phase 3 (curviness scoring).
