# Stars into Monolith Implementation Plan (Option A: hybrid)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give the `stargazer` dark-sky pipeline a public face inside the monolith: a `stars` domain that ingests the pipeline's ranked output into Postgres on a schedule and serves a neobrutalist map of stargazing sites at `jomcgi.dev/app/stars`. The geospatial pipeline itself stays exactly where it is.

**Architecture (Option A, hybrid):** The stargazer CronJob (every 6 hours, `stargazer` namespace, Longhorn PVC) is **untouched**. Its existing in-cluster NGINX API, which serves `best_locations.json` at `/best`, becomes the internal handoff interface. A monolith `shared.scheduler` job (`stars.ingest_sites`, every 3 hours) pulls that JSON over the cluster network and wholesale-replaces a small `stars.sites` table. One SSR-only `/api/stars/sites` endpoint serializes the table; Cloudflare CDN does the viewer fan-out. A SvelteKit route at `/app/stars` renders a MapLibre map of sites colored by astronomy score, with a per-site panel of the best upcoming dark-sky hours.

**Why hybrid, not full absorption:** the pipeline's deps (rasterio, geopandas, osmium) would balloon the monolith image, and its 2Gi+ memory spikes do not belong in the API pod. The pipeline keeps its CronJob and PVC; the monolith only ever touches a few-hundred-KB JSON.

**Tech Stack:** Python 3 / FastAPI / SQLModel / psycopg3 / Postgres (Atlas migrations) / `shared.scheduler` / httpx / SvelteKit (Svelte 5) / MapLibre GL / Bazel + apko.

**Reference implementation:** the `ships` domain (`projects/monolith/ships/*`, `frontend/src/routes/public/app/ships/*`) and, once merged, the `hikes` domain from `docs/plans/2026-06-13-hikes-into-monolith.md`. Hikes lands first; reuse its shapes.

**Non-negotiable house rules (apply to every task):**

- **No em-dashes** in any code, comment, commit, or doc. Use commas, colons, or parentheses.
- **No local test loop.** Write tests, but do NOT run `bazel test`/`pytest`/`vitest` from the workstation. Implement, commit, push the branch, watch CI via `gh pr checks <n> --watch`. Diagnose failures via `mcp__buildbuddy__*`.
- **One code review per PR** at the end, not per task. Implementers self-review before each commit.
- **Conventional Commits** for every commit. Commit frequently (one logical step per commit).
- **No hardcoded `.svc.cluster.local` URLs in code defaults.** The stargazer API URL is injected via `values.yaml` env var; the code default is empty and the job no-ops with a warning when unset (semgrep enforces the URL rule).

**Worktree:** `/tmp/claude-worktrees/stars-monolith` on branch `feat/stars-into-monolith`.

---

## Background: what changes where

**Untouched:** everything under `projects/stargazer/` backend code, the CronJob, the PVC, the NGINX api deployment and its in-cluster Service, the pipeline schedule, the `stargazer` namespace.

**Source of truth for the data contract:** `projects/stargazer/backend/weather.py` `output_best_locations()`. The JSON at NGINX `/best` is a ranked array of:

```json
{
  "id": "<point id>",
  "coordinates": {"lat": 57.1, "lon": -4.7},
  "altitude_m": 312,
  "lp_zone": "dark",
  "best_hours": [
    {"time": "...", "score": 92.5, "cloud_area_fraction": 5, "relative_humidity": 60,
     "wind_speed": 2.1, "air_temperature": 4.0, "dew_spread": 3.1,
     "air_pressure": 1021.0, "symbol": "clearsky_night"}
  ],
  "best_score": 92.5
}
```

(`best_hours` is capped at 5 per site, all hours have score >= 80, sites are sorted by `best_score` desc.)

**Retired (follow-up PR, Task 6):** the public HTTPRoute exposing `api.jomcgi.dev/stargazer`. The NGINX api stays for in-cluster handoff only.

---

## Task 0: Scaffold the `stars` Python package and register it

**Files:**

- Create: `projects/monolith/stars/__init__.py`
- Create: `projects/monolith/stars/router.py` (stub)
- Modify: `projects/monolith/app/main.py` (add `import stars`; `stars.register(app)` after the other `*.register(app)` calls around `:220`; `stars.on_startup_jobs(session)` next to `ships.on_startup_jobs(session)` around `:68`)
- Modify: `projects/monolith/BUILD` (hand-registered tree; copy the `ships`/`hikes` blocks)

```python
def on_startup_jobs(session: Session) -> None:
    """Register the stargazer ingest job."""
    from shared.scheduler import register_job
    from stars.jobs import ingest_sites_handler

    register_job(
        session,
        name="stars.ingest_sites",
        interval_secs=3 * 3600,  # pipeline refreshes 6-hourly; 3h halves worst-case staleness
        handler=ingest_sites_handler,
        ttl_secs=600,
    )
```

Commit: `feat(monolith): scaffold stars package and register router`

---

## Task 1: Postgres schema (migration + SQLModel model)

**Files:**

- Create: `projects/monolith/chart/migrations/20260613000002_stars_schema.sql` (timestamp must sort after the hikes migrations if both are in flight)
- Create: `projects/monolith/stars/models.py`
- Test: `projects/monolith/stars/models_test.py`

**Design:** one table mirroring the `/best` entry shape. `best_hours` stays JSONB (the serving endpoint returns sites whole; no per-hour queries). The table is wholesale-replaced each ingest, so no updated-at bookkeeping per row beyond `fetched_at`.

```sql
CREATE SCHEMA IF NOT EXISTS stars;

CREATE TABLE stars.sites (
    id           TEXT PRIMARY KEY,
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    altitude_m   DOUBLE PRECISION NOT NULL DEFAULT 0,
    lp_zone      TEXT NOT NULL DEFAULT 'unknown',
    best_score   DOUBLE PRECISION NOT NULL,
    best_hours   JSONB NOT NULL DEFAULT '[]'::jsonb,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX sites_best_score ON stars.sites (best_score DESC);
```

SQLModel model follows `ships/models.py` conventions (`schema="stars"`, `extend_existing`, tz-aware datetimes, JSON column via the same approach hikes uses so SQLite tests work). Model test: round-trip one site through a SQLite `create_all` fixture.

Commit: `feat(monolith): add stars postgres schema and model`

---

## Task 2: Ingest job (pull `/best` from the stargazer api)

**Files:**

- Create: `projects/monolith/stars/jobs.py`
- Test: `projects/monolith/stars/jobs_test.py`

**Design:**

- Config: `STARGAZER_API_URL` read via `envOr`-style lookup with **no default**. Unset means log a warning once and return (the job is a no-op until the chart wires the env var; this is the house rule for service URLs).
- Fetch `f"{base_url}/best"` with `httpx` (10s timeout). Validate the payload with a small pydantic row model mirroring the contract above; skip malformed entries with a logged count rather than failing the batch.
- Empty or non-200 responses: log and keep the existing rows (stale beats empty; the pipeline may be mid-run).
- Write path: in one transaction, `DELETE FROM stars.sites` then bulk insert the validated rows with a shared `fetched_at`. The set is small (a Scotland-wide 5 km grid filtered to score >= 80 sites), so wholesale replace is simpler and correct; site ids drift as the grid and weather change, so upsert-and-prune buys nothing.
- Network phase completes before any session use (same handler shape as hikes/ships).

**Tests:** mock httpx; assert happy path replaces rows, malformed entries are skipped, empty/error responses leave existing rows untouched, unset URL no-ops.

Commit: `feat(monolith): stars ingest job pulling stargazer best locations`

---

## Task 3: `/api/stars/sites` endpoint (SSR-only, CDN-cached)

**Files:**

- Modify: `projects/monolith/stars/router.py`
- Test: `projects/monolith/stars/router_test.py`

**Design:** clone `ships/router.py` `get_snapshot`: cache-control constant, ETag from `(count, max(fetched_at))`, conditional GET 304. Payload `{count, fetched_at, sites: [...]}` ordered by `best_score` desc. Cache header: `public, s-maxage=1800, stale-while-revalidate=3600, stale-if-error=86400`.

**Critical:** do **not** add `/api/stars/*` to `chart/templates/httproute-public.yaml`. SSR reaches it at `http://localhost:8000` (same rule as ships and hikes).

**Tests:** SQLite fixture + `TestClient`; assert ordering, headers, 304.

Commit: `feat(monolith): stars sites endpoint (SSR-only, CDN-cached)`

---

## Task 4: Frontend route `/app/stars` (dark-sky map, neobrutalist)

**Files:**

- Create: `projects/monolith/frontend/src/routes/public/app/stars/+page.server.js` (clone ships': fetch `/api/stars/sites`, forward ETag, set `STARS_SITES_CACHE_CONTROL`)
- Create: `projects/monolith/frontend/src/routes/public/app/stars/+page.js` (`export const ssr = false;`)
- Create: `projects/monolith/frontend/src/routes/public/app/stars/+page.svelte`
- Create: `projects/monolith/frontend/src/lib/public/components/stars/StarsMap.svelte`
- Modify: `projects/monolith/frontend/src/lib/cache-headers.js` (add `STARS_SITES_CACHE_CONTROL`, keep-in-sync comment)
- Tests: `page.server.test.js` (clone ships'), plus a pure-module test for any score-bucketing/formatting helpers

**Design:**

- **Map:** MapLibre per `ShipsMap.svelte`, centered on Scotland. This page earns a **dark variant** of the basemap restyle (a dark-sky map should be dark); keep chrome tokens from `design-system.css` so it still reads as a sibling of ships/hikes. Site markers colored by `best_score` buckets (for example 80-85 / 85-92 / 92+), sized or ringed by `lp_zone`.
- **Site panel:** clicking a marker opens a hard-shadow card: coordinates, altitude, light-pollution zone, and a table of the best hours (local time, score, cloud %, temperature, dew spread, weather symbol). Include an "open in maps" link for navigation since these are literally places to drive to.
- **Header bar:** "STARS · n dark-sky sites · best score s" with the `fetched_at` age. If the table is empty (pipeline outage or all-cloudy week), render an honest empty state ("no viewing windows above score 80 in the next 72h") rather than a blank map.
- Snapshot refresh: `setInterval(invalidateAll, 30 * 60_000)` cleared on destroy.

Commit: `feat(frontend): add /app/stars dark-sky map`

> Do not run `pnpm build` (BUILD-clobbering gotcha); let CI build.

---

## Task 5: Chart wiring (env var + bump)

**Files:**

- Modify: `projects/monolith/chart/templates/deployment.yaml` (plain env `STARGAZER_API_URL` from values)
- Modify: `projects/monolith/chart/values.yaml` (empty default) and `projects/monolith/deploy/values.yaml` (the real in-cluster URL)
- Modify: `projects/monolith/chart/Chart.yaml` + `projects/monolith/deploy/application.yaml` (version bump + `targetRevision`, same commit)

**Determining the URL:** the stargazer api Service is templated by the homelab-library helper (`projects/stargazer/chart/templates/service-api.yaml`, component `api`). Render it to get the exact name:

```bash
helm template stargazer projects/stargazer/chart/ -f projects/stargazer/deploy/values.yaml | grep -B2 -A8 "kind: Service"
```

Then set `STARGAZER_API_URL: http://<rendered-service-name>.stargazer.svc.cluster.local` in `deploy/values.yaml` (port per the rendered Service). No secrets are involved.

Also verify network reachability assumptions: monolith and stargazer are different namespaces; if Linkerd policy or NetworkPolicies restrict cross-namespace traffic, add the needed allowance in the stargazer chart values rather than widening anything cluster-wide.

Render check: `helm template monolith projects/monolith/chart/ -f projects/monolith/deploy/values.yaml | grep -A2 STARGAZER_API_URL`.

Commit: `feat(monolith): wire stargazer api url and bump chart for stars`

---

## Task 6: Push, watch CI, end-of-PR review, verify live

1. Push `feat/stars-into-monolith`, open the PR, `gh pr checks <n> --watch`; diagnose via `mcp__buildbuddy__*`, quoting actual errors first.
2. One comprehensive code review of the full diff; address findings.
3. Merge with `gh pr merge --rebase`.
4. Verify live after ArgoCD syncs and the migration applies:
   - Trigger `stars.ingest_sites` via the scheduler skill; confirm `stars.sites` populates and the job log shows the fetch from the stargazer service.
   - `curl -s https://jomcgi.dev/app/stars` renders sites; `curl -I` shows Cache-Control; `/api/stars/sites` is NOT publicly reachable.
   - Confirm the page survives an empty table (check the empty state by eye or by timing before first ingest).

---

## Task 7 (follow-up PR, AFTER live verification): retire the public stargazer route

Only once `/app/stars` is verified live:

- Modify: `projects/stargazer/deploy/values.yaml` to disable the public HTTPRoute (`httproute.enabled: false` or equivalent). The NGINX api Deployment + Service **stay** (they are the ingest handoff).
- Remove any DNS or Cloudflare config pointing `api.jomcgi.dev/stargazer` at the cluster, or redirect it to `https://jomcgi.dev/app/stars`.
- Commit: `chore(stargazer): retire public api route, served via /app/stars`.

---

## Open follow-ups (not blocking)

- A SigNoz HTTP check + alert for `/app/stars` via the `add-httpcheck-alert` skill.
- Ingest `forecasts_scored.json` (`/locations`) too if the page later wants the full sub-80 heatmap rather than only ranked sites.
- A "notify me when a 90+ night is forecast nearby" hook into the monolith notification hub; the data is already in Postgres.
- Longer term: replace the NGINX handoff with the pipeline writing to Postgres directly, but only if the NGINX layer actually causes trouble; today it is the cheapest stable contract.
