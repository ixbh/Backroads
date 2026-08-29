# Putting Backroad on the web

The simplest beta architecture is one FastAPI web service plus one managed
PostgreSQL/PostGIS database. FastAPI now serves both `viewer.html` at `/` and
the route API at `/generate-route`, so there is only one HTTPS origin to
configure and share.

## Recommended first beta: Render

1. Put this folder in a private GitHub, GitLab, or Bitbucket repository. Do not
   commit `.env` or the `.osm.pbf` extract.
2. Create a Render Postgres database in the same region as the web service.
   Enable PostGIS with `CREATE EXTENSION IF NOT EXISTS postgis;`.
3. Restore the existing local database into the managed database. A custom
   format dump preserves the roads and expensive scores:

   ```powershell
   pg_dump --format=custom --no-owner --no-acl $env:DATABASE_URL --file backroad.dump
   pg_restore --no-owner --no-acl --dbname $env:RENDER_DATABASE_URL backroad.dump
   ```

   Keep `backroad.dump` private; it is data, not an application asset.
4. Create a Render Web Service from the repository and select Docker. The
   included `Dockerfile` starts `uvicorn` on Render's assigned `PORT`.
5. Add the managed database's internal connection string as the web service's
   secret `DATABASE_URL`. Do not put it in a frontend setting or commit it.
6. Open `/health`, then the service root URL. Generate a short route before
   inviting testers.
7. Add a custom domain later. Render's generated HTTPS URL is enough for a
   private test; HTTPS is also required for browser geolocation.

Use a paid, non-expiring database for any beta whose data you do not want to
reload. The state import and scoring tables are much larger and slower to
rebuild than the app container.

## Before a public launch

- Set `ALLOWED_ORIGINS` if the frontend is ever moved to a different domain.
- Add request rate limiting and basic tester authentication. Route generation
  is CPU- and memory-intensive enough that an open endpoint can be abused.
- Move graph construction to a persistent cache or routing service so a web
  process restart does not pay the full build cost on its first request.
- Add monitoring, database backups, a privacy policy, and a way to report a
  dangerous or incorrectly tagged road.
- Pin and review basemap/tile-provider usage terms and capacity.
- The built-in Nominatim adapter is intentionally limited to explicit searches,
  one request per second, and a bounded cache. Before a larger/public beta,
  configure `GEOCODER_BASE_URL` for a provider whose capacity and terms match
  the expected traffic.

## iPhone scope

An HTTPS-hosted version works as a route-planning website on iPhone. The
included web manifest and Apple metadata let testers add it to the Home Screen
and open it in a standalone window. That is a useful tester milestone,
but it is not yet a navigation app. Navigation needs GPS tracking, map
matching, maneuver instructions, off-route detection and rerouting, voice
guidance, background behavior, turn restrictions, closures/traffic, and a
navigation-grade routing engine. CarPlay support additionally requires a
native iOS app and Apple's CarPlay navigation entitlement/review.

The safest architecture is to keep this project's explainable scenic scorer as
the route-selection layer, but delegate legal turn-by-turn paths and maneuvers
to a mature routing engine such as Valhalla, GraphHopper, or OSRM rather than
trying to turn the current NetworkX prototype into the entire navigation stack.

## Windows desktop beta

`BackroadBeta.spec` packages the same API and viewer into a portable Windows
folder. `export_desktop_data.py` exports only runtime road geometry and
precomputed scores to an immutable SQLite database with an RTree bounding-box
index. This shifts NetworkX graph construction onto each tester's computer and
avoids hosted free-tier CPU and RAM limits without altering the PostgreSQL
source database.

The portable folder must be distributed as a whole because the routing data is
intentionally external to the executable. A one-file executable would extract
the large database into a temporary directory on every launch. The portable
zip can later be wrapped in an installer without changing the runtime layout.

The desktop beta is Windows-only and is not a substitute for the hosted iPhone
test. It still requires internet access for OpenFreeMap tiles. Distribution
must retain `DATA_LICENSE.txt`, the in-map attribution, and the ODbL terms for
the included OpenStreetMap-derived database.
