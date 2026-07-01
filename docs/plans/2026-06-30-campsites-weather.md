# Campsites x Weather (BC Parks) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship a public page at `jomcgi.dev/app/campsites` that shows, for every reservable BC Parks campground, which of the next 14 days have campsite availability AND clear-sky ("good camping / stargazing") weather, on a MapLibre map plus a sortable list.

**Architecture:** A new monolith domain package `campsites/` follows the `hikes`/`worldcup`/`dr_jobs` pattern: an hourly Argo CronWorkflow (`campsites-refresh`) polls the BC Parks GoingToCamp availability API and the Open-Meteo forecast API, computes a per-park-per-date `sunny_score`, and upserts three Postgres tables in a `campsites` schema. The `campsites` schema is granted to `public_reader` (wholly public data, like `worldcup`). A read-only SSR endpoint `/api/campsites/snapshot` backs a SvelteKit page under `routes/public/app/campsites/` that renders a map (reusing the `ships` MapLibre stack) plus a ranked list, deep-linking each park to the official booking site for the actual reservation.

**Tech Stack:** Python 3 / FastAPI / SQLModel / httpx (async) / Typer job entrypoint / Argo CronWorkflow / Postgres (CNPG) / SvelteKit (Svelte 5 runes) / MapLibre GL / Open-Meteo / apko+Bazel (CI only).

---

## Validated recon (do not re-derive; confirmed against the live site 2026-06-30)

BC Parks reservations run on the **GoingToCamp / Aspira** platform at `camping.bcparks.ca`, fronted by an **Azure WAF**.

- **WAF bypass:** a plain server request gets `403` (Azure WAF HTML). Sending browser-like headers returns `200`. Required headers on every call:
  - `User-Agent: <browser-like UA>` (use an honest, contactable UA, see Task 2)
  - `Referer: https://camping.bcparks.ca/`
  - `Origin: https://camping.bcparks.ca`
  - `Accept: application/json, text/plain, */*`, `Accept-Language: en-CA,en;q=0.9`, `sec-fetch-*` cors headers.
  - No auth, no cookie handshake, no JS/captcha challenge is needed to READ availability. (Booking would need auth; we never book.)
- **Catalog (static, refresh weekly):**
  - `GET https://camping.bcparks.ca/api/resourceLocation` -> JSON list of ~145 campgrounds. Each entry has `resourceLocationId` (negative int32, stable), `gpsCoordinates` (lat/lon), `ianaTimeZone`, `region`, `localizedValues[].shortName/fullName/description`, `googleAddress`.
  - `GET https://camping.bcparks.ca/api/maps` -> the map hierarchy. Top-level array; walk `mapLinks[]` to find entries where `resourceLocationId` is set: each gives `(childMapId, resourceLocationId, localizations[].title)`. The `childMapId` is the **park map id** you pass as `mapId` to the availability endpoint. Build a map of `resourceLocationId -> parkMapId` (+ human title).
- **Availability (hourly):** for each park:
  ```
  GET https://camping.bcparks.ca/api/availability/map
    ?mapId=<parkChildMapId>
    &resourceLocationId=<resourceLocationId>
    &startDate=<today>            # YYYY-MM-DD, park-local
    &endDate=<today+14>
    &nights=1
    &getDailyAvailability=true
    &equipmentCategoryId=-32768   # non-group / any
    &partySize=4
  ```
  Returns `mapLinkAvailabilities: { "<loopMapId>": [d0, d1, ... d14] }` where each array is one 0/1 flag PER DATE across the window (validated: a 15-day window returned 15-element arrays like `[0,0,1,1,0,...]`). **Per-park-per-date availability = logical OR across all the park's loop arrays at that date index.** ONE request per park covers the whole 14-day window. Total ~151 requests/hour.
- **Weather (hourly):** Open-Meteo, free, no key, covers Canada:
  ```
  GET https://api.open-meteo.com/v1/forecast
    ?latitude=<lat>&longitude=<lon>
    &daily=cloud_cover_mean,precipitation_sum,precipitation_probability_max,temperature_2m_max,wind_speed_10m_max
    &forecast_days=14
    &timezone=<campground ianaTimeZone>
  ```
  Returns `daily.time[]` plus parallel arrays for each variable. Align each `daily.time[i]` date to the availability date. Only the ~14-day horizon has a forecast, which is exactly the product scope.

**Politeness / safety (encode in the client, Task 2):** single cluster egress IP means any ban is global, so: hourly cadence only, loop-level only (no per-site drilling), small per-request jitter, sequential-with-small-delay (not a burst), exponential backoff + a per-run circuit breaker that STOPS the run on repeated `403/429/5xx` rather than retrying into a ban. Never poll the 07:00 PT reservation-opening surge aggressively (hourly at `:00` UTC is fine, it is not aligned to 07:00 PT).

---

## Conventions this plan reuses (verified in `projects/monolith`)

- **Closest sibling: `hikes/`** (scheduled scrape + async httpx forecast fetch in `hikes/forecast.py` + SSR public page). Read it before starting.
- Domain package exports `register(app)`, `register_public = register` (wholly public), and `on_startup_jobs(session)` (guarded by `scheduler.api.argo_handled(...)`; the in-process scheduler is retired, the Argo CronWorkflow is the operative path, but keep the hook for parity).
- Job one-shot command in `app/jobs_main.py` via `_run_job(name, import_path, handler_name)` (line 170). Example: `_run_job("stars-refresh", "stars.jobs", "refresh_handler")`.
- Handler contract: `async def handler(session) -> datetime | None`. Do network I/O with `await httpx...`; do ALL DB writes inside `await asyncio.to_thread(_sync_helper, data)` where `_sync_helper` opens its own `Session(get_engine())` and commits. Return `None`.
- Schema via timestamped migrations in `projects/monolith/chart/migrations/` (`YYYYMMDDHHMMSS_<name>.sql`), plus a separate `..._public_reader_grant.sql`. SQLModel classes mirror the migration with `__table_args__ = {"schema": "campsites", "extend_existing": True}`.
- Public read endpoint sets `Cache-Control` (public, `s-maxage`, `stale-while-revalidate`, `stale-if-error`) + an `ETag` and returns `304` on `if-none-match`. See `ships/router.py`.
- Register in `app/main.py` (`campsites.register(app)`, near the other `*.register(app)` block ~line 242) and `app/main_public.py` (`campsites.register_public(app)`, ~line 46). Add `import campsites` near the other domain imports in both.
- Argo CronWorkflow entry in `projects/monolith/chart/values.yaml` under `jobs.cronWorkflows`.
- Deploy: bump `projects/monolith/chart/Chart.yaml` `version` AND `projects/monolith/deploy/application.yaml` `targetRevision` in sync. Run `format`. No local tests: push and watch BuildBuddy CI.

**Testing note (repo rule):** there is NO local test loop. Write unit tests alongside pure-logic code (they are the review artifact and run in CI), but DO NOT run `pytest`/`bazel test` locally. "Run the test" steps below mean: the test exists and is expected to pass in end-of-plan CI. Verify pure logic by reading, and by `helm template` / `format` where applicable.

---

### Task 1: Schema, grant migration, and SQLModel models

**Files:**

- Create: `projects/monolith/chart/migrations/20260630150000_campsites_schema.sql`
- Create: `projects/monolith/chart/migrations/20260630150100_campsites_public_reader_grant.sql`
- Create: `projects/monolith/campsites/__init__.py` (stub `register`/`register_public`/`on_startup_jobs`, filled in later tasks; for now just the package + imports so migrations/models can be referenced)
- Create: `projects/monolith/campsites/models.py`
- Reference: `projects/monolith/chart/migrations/20260620120000_worldcup_schema.sql`, `20260620120100_worldcup_public_reader_grant.sql`, `projects/monolith/worldcup/models.py`

**Step 1: Write the schema migration.** Three tables in schema `campsites`:

```sql
-- Campsites x weather: BC Parks (GoingToCamp) availability joined to Open-Meteo
-- forecast for the /app/campsites page. Wholly public data; a companion grant
-- migration exposes the schema to public_reader (see ..._public_reader_grant.sql).
CREATE SCHEMA IF NOT EXISTS campsites;

-- Static catalog, refreshed weekly from /api/resourceLocation + /api/maps.
CREATE TABLE campsites.campgrounds (
    resource_location_id BIGINT PRIMARY KEY,          -- stable negative int32 from GoingToCamp
    park_map_id          BIGINT NOT NULL,             -- childMapId to pass as mapId for availability
    name                 TEXT   NOT NULL,
    region               TEXT   NOT NULL DEFAULT '',
    latitude             DOUBLE PRECISION NOT NULL,
    longitude            DOUBLE PRECISION NOT NULL,
    iana_tz              TEXT   NOT NULL DEFAULT 'America/Vancouver',
    description          TEXT   NOT NULL DEFAULT '',
    booking_url          TEXT   NOT NULL DEFAULT 'https://camping.bcparks.ca/',
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-park-per-date availability (loop-level OR), upserted hourly.
CREATE TABLE campsites.availability (
    resource_location_id BIGINT NOT NULL REFERENCES campsites.campgrounds(resource_location_id) ON DELETE CASCADE,
    date                 DATE   NOT NULL,
    has_availability     BOOLEAN NOT NULL DEFAULT FALSE,
    loops_open           INTEGER NOT NULL DEFAULT 0,   -- how many loops had a site that date
    scraped_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (resource_location_id, date)
);
CREATE INDEX idx_campsites_avail_date ON campsites.availability (date);

-- Per-park-per-date forecast + computed sunny score, upserted hourly.
CREATE TABLE campsites.weather (
    resource_location_id BIGINT NOT NULL REFERENCES campsites.campgrounds(resource_location_id) ON DELETE CASCADE,
    date                 DATE   NOT NULL,
    cloud_cover          DOUBLE PRECISION,             -- mean %, 0..100
    precip_sum           DOUBLE PRECISION,             -- mm
    precip_prob          INTEGER,                      -- max %, 0..100
    temp_max             DOUBLE PRECISION,             -- deg C
    wind_max             DOUBLE PRECISION,             -- km/h
    sunny_score          INTEGER NOT NULL DEFAULT 0,   -- 0..100, clear-sky-priority
    is_good              BOOLEAN NOT NULL DEFAULT FALSE,
    fetched_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (resource_location_id, date)
);
```

**Step 2: Write the public_reader grant migration** (copy the `worldcup` grant wording):

```sql
-- Grant public_reader read access to campsites (ADR 004). The /app/campsites page
-- is served by the public tier (monolith-public) reading monolith-pg-ro as
-- public_reader. BC Parks availability + Open-Meteo forecast are wholly public
-- data, so campsites joins hikes/ships/stars/dr_jobs/worldcup as a directly
-- readable schema (no public_api view needed). ALTER DEFAULT PRIVILEGES covers
-- future tables. See 20260617000000_public_reader_role.sql.
GRANT USAGE ON SCHEMA campsites TO public_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA campsites TO public_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA campsites
    GRANT SELECT ON TABLES TO public_reader;
```

**Step 3: Write `campsites/models.py`** mirroring the migration (SQLModel `table=True`, `__table_args__ = {"schema": "campsites", "extend_existing": True}`). Classes: `Campground`, `Availability`, `Weather`. Use `date` typed as `datetime.date`, timestamps as `datetime`. Composite PKs via two `Field(primary_key=True)`.

**Step 4: Write `campsites/__init__.py` stub:**

```python
"""campsites: BC Parks availability x clear-sky weather (/app/campsites)."""
from fastapi import FastAPI
from sqlmodel import Session


def register(app: FastAPI) -> None:
    from campsites.router import router
    app.include_router(router)


register_public = register  # wholly public, read-only


def on_startup_jobs(session: Session) -> None:
    from scheduler.api import argo_handled, register_job
    if argo_handled("campsites.refresh"):
        return
    from campsites.jobs import refresh_handler
    register_job(session, name="campsites.refresh", interval_secs=3600,
                 handler=refresh_handler, ttl_secs=600)
```

(`router`/`jobs` land in later tasks; the lazy imports keep this import-safe until then.)

**Step 5: Update the Atlas migration checksum.** The migrations dir has an `atlas.sum` the operator verifies. After adding SQL files run `format` (see Task 7) or the repo's atlas-hash step and confirm `atlas.sum` includes the two new files. GOTCHA: a stale `atlas.sum` fails the migration job silently in prod. Verify the two new filenames appear in `chart/migrations/atlas.sum` before committing.

**Step 6: Commit.**

```bash
git add projects/monolith/chart/migrations/20260630150000_campsites_schema.sql \
        projects/monolith/chart/migrations/20260630150100_campsites_public_reader_grant.sql \
        projects/monolith/chart/migrations/atlas.sum \
        projects/monolith/campsites/__init__.py projects/monolith/campsites/models.py
git commit -m "feat(campsites): schema, public_reader grant, and SQLModel models"
```

---

### Task 2: BC Parks GoingToCamp client (catalog + availability, WAF-safe, backoff)

**Files:**

- Create: `projects/monolith/campsites/bcparks.py`
- Test: `projects/monolith/campsites/bcparks_test.py`
- Reference: `hikes/forecast.py` (async httpx fetch + graceful-None-on-error shape)

**Step 1: Write failing unit tests for the PURE parsers** (no network):

- `test_parse_catalog`: given a small `resourceLocation` JSON fixture + a `maps` JSON fixture, `parse_catalog(resource_json, maps_json)` returns a list of `CampgroundRow(resource_location_id, park_map_id, name, region, latitude, longitude, iana_tz, description, booking_url)`. Assert IDs, coords, tz, and that a `resourceLocationId` with no matching park map id is dropped (logged).
- `test_availability_or_merge`: given a `mapLinkAvailabilities` dict `{"-1":[0,1,0], "-2":[1,1,0]}` and a `start_date`, `merge_availability(payload, start_date, ndays=3)` returns `[(date0, open=True, loops_open=1), (date1, open=True, loops_open=2), (date2, open=False, loops_open=0)]`. Assert the OR and the per-date loop count.
- `test_availability_ragged`: arrays of differing lengths or missing days do not crash (missing index -> treated as 0/closed).

**Step 2: Implement `bcparks.py`.** Key pieces:

```python
import asyncio, logging, os
from dataclasses import dataclass
from datetime import date, timedelta
import httpx

logger = logging.getLogger("monolith.campsites.bcparks")

BASE = "https://camping.bcparks.ca"
# Honest, contactable UA: identifies the tool + a contact so a human treats a
# polite hourly reader as such. Browser tokens are required to pass Azure WAF.
UA = ("Mozilla/5.0 (compatible; jomcgi-campsites/1.0; +https://jomcgi.dev/app/campsites; "
      "hourly clear-sky availability reader) Chrome/126 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-CA,en;q=0.9",
    "Referer": f"{BASE}/",
    "Origin": BASE,
    "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
}
EQUIPMENT_ANY = -32768
WINDOW_DAYS = 14
INTER_REQUEST_DELAY_S = 0.4     # polite pacing between parks
MAX_CONSECUTIVE_FAILURES = 5    # circuit breaker: stop the run, do not ban ourselves
```

- `parse_catalog(resource_json, maps_json) -> list[CampgroundRow]`: build `resourceLocationId -> (park_map_id, title)` by walking `maps_json` `mapLinks`; join to `resourceLocation` entries for coords/tz/region/description. Pick `shortName` for name, fall back to `title`. `booking_url` = `f"{BASE}/"` (refine to a per-park deep link in Task 6 if a slug is available; otherwise the site's park picker).
- `merge_availability(payload, start_date, ndays=WINDOW_DAYS) -> list[DayAvail]`: OR across `mapLinkAvailabilities` arrays, count loops open per index, map index -> `start_date + timedelta(days=i)`.
- `async def fetch_catalog(client) -> list[CampgroundRow]`: two GETs (`/api/resourceLocation`, `/api/maps`), then `parse_catalog`.
- `async def fetch_availability(client, cg: CampgroundRow, start_date) -> list[DayAvail] | None`: GET `/api/availability/map` with the params above; return `None` on error (caller counts failures).
- `async def fetch_all_availability(client, cats, start_date) -> dict[int, list[DayAvail]]`: sequential loop with `await asyncio.sleep(INTER_REQUEST_DELAY_S + jitter)`, exponential backoff on a failed park (retry once after 2s), and a circuit breaker: if `consecutive_failures >= MAX_CONSECUTIVE_FAILURES`, log an error and RETURN what we have (do not keep hammering a WAF that is blocking us). Jitter via `random` seeded by index (no wall-clock randomness needed; `random.Random(i).uniform(0,0.3)`).

Use a single `httpx.AsyncClient(headers=HEADERS, timeout=25, follow_redirects=True)`.

**Step 3-4:** Tests exist and are expected to pass in CI. Verify parsers by reading.

**Step 5: Commit.**

```bash
git add projects/monolith/campsites/bcparks.py projects/monolith/campsites/bcparks_test.py
git commit -m "feat(campsites): WAF-safe GoingToCamp catalog + availability client"
```

---

### Task 3: Open-Meteo weather client + `sunny_score` (pure, TDD)

**Files:**

- Create: `projects/monolith/campsites/weather.py`
- Test: `projects/monolith/campsites/weather_test.py`

**Step 1: Write failing tests for `sunny_score` (the product core).** The score is clear-sky-priority (stargazer ethos), dry-gated, with a temp-comfort adjustment. Named constants:

```python
# sunny_score: clear skies dominate (good stargazing + pleasant days), penalized
# by rain risk, nudged by temperature comfort. 0..100.
CLEAR_WEIGHT = 1.0            # base = (100 - cloud_cover_mean) * CLEAR_WEIGHT
PRECIP_PENALTY_PER_PCT = 0.4 # subtract precip_prob(%) * this
PRECIP_PENALTY_PER_MM  = 8.0 # subtract precip_sum(mm) * this  (capped)
PRECIP_PENALTY_CAP     = 45.0
COMFORT_LO, COMFORT_HI = 15.0, 28.0   # deg C: no temp penalty inside this band
TEMP_PENALTY_PER_DEG   = 2.0          # per deg outside the band, capped
TEMP_PENALTY_CAP       = 20.0
# is_good gate (used for map "good day" coloring):
GOOD_SCORE_MIN   = 60
GOOD_PRECIP_MAX_MM = 3.0
```

Test cases:

- Clear + dry + mild (cloud=5, precip=0, prob=0, temp=22) -> score ~95, `is_good True`.
- Overcast (cloud=90, ...) -> low score, `is_good False`.
- Clear but rainy (cloud=10, precip=6mm, prob=80) -> heavy precip penalty pulls score down, `is_good False` (precip gate).
- Clear + dry but cold (cloud=10, temp=2) -> temp penalty, still maybe good-ish but below gate if penalty large enough. Assert exact number from the formula.
- Clamping: never <0 or >100.

**Step 2: Implement.**

```python
def sunny_score(cloud_cover, precip_sum, precip_prob, temp_max) -> int:
    base = (100.0 - (cloud_cover or 0.0)) * CLEAR_WEIGHT
    precip = min(PRECIP_PENALTY_CAP,
                 (precip_prob or 0) * PRECIP_PENALTY_PER_PCT
                 + (precip_sum or 0.0) * PRECIP_PENALTY_PER_MM)
    if temp_max is None:
        temp_pen = 0.0
    elif temp_max < COMFORT_LO:
        temp_pen = min(TEMP_PENALTY_CAP, (COMFORT_LO - temp_max) * TEMP_PENALTY_PER_DEG)
    elif temp_max > COMFORT_HI:
        temp_pen = min(TEMP_PENALTY_CAP, (temp_max - COMFORT_HI) * TEMP_PENALTY_PER_DEG)
    else:
        temp_pen = 0.0
    return max(0, min(100, round(base - precip - temp_pen)))


def is_good_day(score, precip_sum) -> bool:
    return score >= GOOD_SCORE_MIN and (precip_sum or 0.0) < GOOD_PRECIP_MAX_MM
```

- `parse_forecast(daily_json, ndays) -> list[WxDay]`: zip `daily.time[]` with the five variable arrays into per-date rows, computing `sunny_score` + `is_good`.
- `async def fetch_forecast(client, lat, lon, tz) -> dict | None`: GET the Open-Meteo URL (no key), return JSON or `None` on error.
- `async def fetch_all_weather(client, cats) -> dict[int, list[WxDay]]`: one request per campground (Open-Meteo is generous and keyless; still pace with a small sleep + backoff). Optionally note a future optimization: Open-Meteo accepts comma-separated `latitude`/`longitude` for bulk, but start simple (one call each) for clarity.

**Step 5: Commit.**

```bash
git add projects/monolith/campsites/weather.py projects/monolith/campsites/weather_test.py
git commit -m "feat(campsites): Open-Meteo forecast client and clear-sky sunny_score"
```

---

### Task 4: Refresh job (catalog weekly + availability/weather hourly), Typer command, CronWorkflow

**Files:**

- Create: `projects/monolith/campsites/jobs.py`
- Modify: `projects/monolith/app/jobs_main.py` (add a `campsites-refresh` command near the other `_run_job` commands, ~line 206)
- Modify: `projects/monolith/chart/values.yaml` (add `jobs.cronWorkflows` entry)
- Test: `projects/monolith/campsites/jobs_test.py` (sync upsert helpers with a SQLite/in-memory or fixture session, mirroring how `hikes`/`worldcup` tests structure `_sync_helper` tests)

**Step 1: Write `refresh_handler`.**

```python
async def refresh_handler(session) -> datetime | None:
    """Hourly: refresh availability + weather for all BC Parks campgrounds.
    Catalog is refreshed only when stale (>6 days) or empty."""
    start = date.today()  # scheduler provides no date; today in UTC is fine, availability is day-granular
    async with httpx.AsyncClient(headers=bcparks.HEADERS, timeout=25, follow_redirects=True) as gc:
        cats = await asyncio.to_thread(_load_catalog_rows)  # from DB
        if _catalog_stale(cats):
            cats = await bcparks.fetch_catalog(gc)
            await asyncio.to_thread(_upsert_catalog, cats)
        avail = await bcparks.fetch_all_availability(gc, cats, start)
    async with httpx.AsyncClient(timeout=25) as om:
        wx = await weather.fetch_all_weather(om, cats)
    await asyncio.to_thread(_upsert_availability, avail, start)
    await asyncio.to_thread(_upsert_weather, wx)
    return None
```

- `_catalog_stale`: empty, or `max(updated_at) < now - 6 days`.
- Sync helpers each open `Session(get_engine())`, upsert (delete-and-insert per park for availability/weather over the window, or `INSERT ... ON CONFLICT DO UPDATE`), and commit. Prune `availability`/`weather` rows with `date < today` in the same pass (14-day rolling window stays small).
- Guard the WHOLE handler so a partial failure (e.g. WAF blocks availability but weather succeeds) still commits what it got and logs, never leaving the tables empty.

**Step 2: Add the Typer command** in `app/jobs_main.py`:

```python
@app.command("campsites-refresh")
def campsites_refresh() -> None:
    """Refresh BC Parks availability + Open-Meteo forecast for /app/campsites."""
    _run_job("campsites-refresh", "campsites.jobs", "refresh_handler")
```

**Step 3: Add the CronWorkflow** in `chart/values.yaml` under `jobs.cronWorkflows` (match the sibling shape incl. `resources`):

```yaml
- name: campsites-refresh
  replaces: campsites.refresh
  args: ["campsites-refresh"]
  schedule: "0 * * * *" # hourly at :00 UTC (not aligned to 07:00 PT surge)
  concurrencyPolicy: Forbid
  activeDeadlineSeconds: 900 # ~151 paced availability calls + ~145 weather calls
  suspend: false
  resources:
    requests:
      cpu: 250m
      memory: 512Mi
    limits:
      memory: 512Mi
```

**Step 4:** Verify the chart still renders (Task 7 runs `helm template`).

**Step 5: Commit.**

```bash
git add projects/monolith/campsites/jobs.py projects/monolith/campsites/jobs_test.py \
        projects/monolith/app/jobs_main.py projects/monolith/chart/values.yaml
git commit -m "feat(campsites): hourly refresh job, Typer command, and Argo CronWorkflow"
```

---

### Task 5: Public read endpoint + app registration

**Files:**

- Create: `projects/monolith/campsites/router.py`
- Modify: `projects/monolith/app/main.py` (add `import campsites` near line 15-23; `campsites.register(app)` near line 242)
- Modify: `projects/monolith/app/main_public.py` (add `import campsites`; `campsites.register_public(app)` near line 46)
- Test: `projects/monolith/campsites/router_test.py`
- Reference: `ships/router.py` (ETag/304 + cache headers), `worldcup/router.py`

**Step 1: Write `GET /api/campsites/snapshot`.** Router `prefix="/api/campsites"`. Join `campgrounds` x `availability` x `weather` over the next 14 days. Payload:

```json
{
  "generated_at": "<max scraped_at ISO>",
  "count": 151,
  "parks": [
    {
      "id": -2147483606,
      "name": "Golden Ears",
      "region": "...",
      "lat": 49.36,
      "lon": -122.47,
      "booking_url": "https://camping.bcparks.ca/",
      "best_score": 88, // max sunny_score among AVAILABLE days (0 if none) -> map color
      "good_days": 3, // count of days where available AND is_good
      "days": [
        {
          "date": "2026-06-30",
          "available": true,
          "sunny_score": 88,
          "is_good": true,
          "cloud": 12,
          "precip": 0.0,
          "temp_max": 24.1
        }
      ]
    }
  ]
}
```

- `best_score` / `good_days` are computed server-side so the map + list sort need no client math.
- Cache headers like ships: `public, max-age=0, s-maxage=1800, stale-while-revalidate=3600, stale-if-error=86400` (hourly data, 30 min edge cache is fine). ETag from `(generated_at, count)`; return `304` on match.
- If tables are empty, return `503` (page shows a friendly "warming up" state).

**Step 2: Register** in both `main.py` and `main_public.py` (imports + calls, matching the surrounding style).

**Step 3: Tests** assert payload shape and the `best_score`/`good_days` derivation from a seeded fixture.

**Step 5: Commit.**

```bash
git add projects/monolith/campsites/router.py projects/monolith/campsites/router_test.py \
        projects/monolith/app/main.py projects/monolith/app/main_public.py
git commit -m "feat(campsites): public snapshot endpoint and app registration"
```

---

### Task 6: Frontend, MapLibre map + ranked list at /app/campsites

**Files:**

- Create: `projects/monolith/frontend/src/routes/public/app/campsites/+page.server.js`
- Create: `projects/monolith/frontend/src/routes/public/app/campsites/+page.svelte`
- Create: `projects/monolith/frontend/src/lib/public/components/campsites/CampsitesMap.svelte`
- Reference: `frontend/src/routes/public/app/ships/+page.server.js` + `+page.svelte`, `frontend/src/lib/public/components/ships/ShipsMap.svelte`, `frontend/src/routes/public/app/dr-jobs/+page.svelte` (list/table styling)

**Step 1: `+page.server.js`** SSR load from `${API_BASE}/api/campsites/snapshot` (`API_BASE = process.env.API_BASE || "http://localhost:8000"`), timeout 10s, `throw error(503, ...)` on non-ok, set cache-control + versioned ETag exactly like `ships/+page.server.js`, return `{ snapshot }`.

**Step 2: `CampsitesMap.svelte`** modeled on `ShipsMap.svelte`:

- Lazy-import `maplibre-gl` in `onMount` (browser-only), `import "maplibre-gl/dist/maplibre-gl.css"`.
- Basemap `https://tiles.openfreemap.org/styles/liberty`, center `[-125, 54]` zoom `4.6` (BC), fit bounds to parks.
- One GeoJSON source of parks; a `circle` layer colored by `best_score` via a `["interpolate", ["linear"], ["get","best_score"], 0,"#6b7280", 40,"#eab308", 70,"#f59e0b", 90,"#22c55e"]` ramp (grey = nothing open/clear, green = open + clear). Radius grows slightly with `good_days`.
- Click a park -> emit selection (Svelte 5 `$props` callback or a bound `$state`) so the page shows that park's 14-day detail. Popup shows name + best_score + good_days.
- `propagateTraceHeaderCorsUrls: []` NOT relevant here (no OTEL fetch instrumentation on tiles), but keep the openfreemap tiles same as ships to avoid the CORS-preflight tile-blanking issue documented for ships.

**Step 3: `+page.svelte`** layout: map on the left/top, a ranked list beside/below.

- Svelte 5 runes: `let { data } = $props();` `let parks = $derived(data.snapshot?.parks ?? []);`
- List sortable by `best_score` (default), `good_days`, name, region; filter by region and a "clear nights only" (`good_days > 0`) toggle.
- Selecting a park (from map or list) opens a 14-day strip: each day cell shows availability (open/closed) shaded by `sunny_score`, with cloud/precip/temp on hover, and a "Book on BC Parks" link to `booking_url`.
- Header line: "Availability as of {generated_at}. Weather forecast, next 14 days." (live-at-snapshot framing).
- `onMount`: `setInterval(() => invalidateAll(), 15 * 60_000)` to re-pull hourly-ish (data only changes hourly; 15 min is plenty).
- `<svelte:head>` title + meta description.

**Step 4: Booking deep-link spike (small).** Check whether `resourceLocation`/`maps` exposes a per-park slug/url usable for a deep link (e.g. `camping.bcparks.ca/create-booking/...`). If yes, set `booking_url` per park in Task 2's `parse_catalog`; if not, leave the site root. Do not block the task on this.

**Step 5: Commit.**

```bash
git add projects/monolith/frontend/src/routes/public/app/campsites/ \
        projects/monolith/frontend/src/lib/public/components/campsites/
git commit -m "feat(campsites): MapLibre map + ranked list page at /app/campsites"
```

---

### Task 7: Deploy wiring, format, render checks

**Files:**

- Modify: `projects/monolith/chart/Chart.yaml` (`version` bump, minor: this is a new feature)
- Modify: `projects/monolith/deploy/application.yaml` (`targetRevision` = new version)
- Possibly: `docs/` note + repo_docs manifest regen if a doc is added

**Step 1:** Confirm no new public route allow-list change is needed. `chart/templates/httproute-public.yaml` routes `/` to the public backend and pages live under `routes/public/app/`, so `/app/campsites` is served with no HTTPRoute edit. VERIFY by reading `httproute-public.yaml`; only add a rule if a narrowly-scoped `/api/...` prefix list exists that must include `/api/campsites`.

**Step 2:** Bump `Chart.yaml` `version` and set `deploy/application.yaml` `targetRevision` to the same value (chart-version-bot normally does this on push, but set both to keep them in sync; a mismatch means ArgoCD keeps the old chart).

**Step 3:** Run `format` (formats Python/JS, updates BUILD files via gazelle, and should refresh `atlas.sum`). Confirm no stray diffs.

**Step 4:** Render locally (no install):

```bash
helm template monolith projects/monolith/chart/ -f projects/monolith/deploy/values.yaml \
  | grep -A2 campsites-refresh
```

Expected: the CronWorkflow object for `campsites-refresh` appears.

**Step 5:** If a docs markdown was added for the app, run `bazel run //projects/monolith:gen_repo_docs_manifest` and commit the regenerated manifest (CI enforces freshness). If no doc added, skip.

**Step 6: Commit + push + PR.**

```bash
git add projects/monolith/chart/Chart.yaml projects/monolith/deploy/application.yaml
git commit -m "chore(campsites): bump chart version and wire deploy"
git push -u origin feat/bc-camping-weather
gh pr create --fill
```

---

## End-of-plan verification (CI is the test loop)

1. Push the branch; watch `gh pr checks <n> --watch`.
2. On red CI: `mcp__buildbuddy__get_invocation` (commitSha selector) -> `get_target` -> `get_log`; quote the actual failing assertion before hypothesizing (repo rule). Common misses: SQLModel/migration column drift, a numeric constant a test asserts on, gazelle BUILD drift (rerun `format`), `atlas.sum` not updated.
3. One comprehensive code review of the full PR diff at the end (Opus reviewer), NOT per task. Focus: WAF header correctness, circuit-breaker actually stops on repeated 403, `asyncio.to_thread` used for all DB writes (no event-loop blocking), `sunny_score` matches the documented formula, public_reader grant present, Chart/targetRevision in sync.
4. After merge (rebase): confirm the migration applied, the first `campsites-refresh` pod ran green in `monolith-workflows`, and `jomcgi.dev/app/campsites` renders with data. Verify the poller is NOT getting WAF-403 in prod logs (single egress IP): `kubectl logs` the job pod for a sample of `200`s.

## Post-merge memory to save (durable, non-obvious)

Write a `project_campsites_bcparks.md` memory: the GoingToCamp endpoints + Azure-WAF-header bypass + `getDailyAvailability` per-date-array trick + Open-Meteo clear-sky join, so a future session does not re-probe. Keep the MEMORY.md index line short (the index is already near its size cap).
