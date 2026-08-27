# Phase 3 — Curviness Scoring

Reads the road geometry Phase 2 already loaded and computes a curviness
score for every road. No Google APIs, no new installs beyond two small
Python geometry libraries.

## 1. Install the two new dependencies

```powershell
pip install shapely pyproj
```

## 2. Run it against your real data

Same `DATABASE_URL` you've already got set in this terminal session. First,
clear out any partial results from the earlier run that got interrupted:

```powershell
& "F:\postgres\bin\psql.exe" -U postgres -h localhost -d routes -c "DELETE FROM road_scores;"
```

Then run it:

```powershell
python curviness.py --region minnesota
```

This version logs progress every 1,000 roads (not 5,000), so you'll see
continuous movement instead of a long silent stretch. For your real ~300K
roads, expect somewhere in the 3-6 minute range total — a live, corrected
test on 100,000 realistic-length synthetic roads completed in about a
minute. If it runs noticeably longer than that with no progress lines
moving, that's worth flagging rather than assuming it's just slow.

## 3. Sanity-check it the way that actually matters: against roads you know

Pull the top 20 curviest roads in the state and see if they're places you'd
actually expect:

```powershell
& "F:\postgres\bin\psql.exe" -U postgres -h localhost -d routes -c "
SELECT r.name, r.highway, round(r.length_m::numeric,0) AS length_m, rs.curviness_score
FROM roads r JOIN road_scores rs ON rs.road_id = r.id
WHERE r.region = 'minnesota' AND r.length_m > 500
ORDER BY rs.curviness_score DESC
LIMIT 20;
"
```

And the flattest, to make sure interstates and grid streets land at the
bottom where they should:

```powershell
& "F:\postgres\bin\psql.exe" -U postgres -h localhost -d routes -c "
SELECT r.name, r.highway, rs.curviness_score
FROM roads r JOIN road_scores rs ON rs.road_id = r.id
WHERE r.region = 'minnesota' AND r.length_m > 500
ORDER BY rs.curviness_score ASC
LIMIT 20;
"
```

If a road pops up near the top that you actually know and know to be boring
(or vice versa), that's useful signal — tell me which one and we can look at
why.

## What this does NOT do yet

- No scenery, elevation, or composite score yet — those are Phases 4 and 5.
- `curviness_raw` and `urban_conflict_penalty` are stored separately in
  `road_scores`; they get combined multiplicatively into a final composite
  score in Phase 5, not merged here.
- The scoring constants (sweeper/moderate/hairpin thresholds, weighting,
  urban decay rate) are all named constants at the top of `curviness.py` —
  worth tuning once you've eyeballed real Minnesota results, rather than
  treating the current numbers as final.
