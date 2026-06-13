# Hikes into Monolith Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Migrate the standalone `projects/hikes/` app (manual Bazel-run scrapers + SQLite-in-git + Cloudflare Pages frontend + R2 brotli bundle) into the monolith as a `hikes` domain with Postgres state, scheduled scrape/forecast jobs, and a public, SSR-sourced, CDN-cached neobrutalist page at `jomcgi.dev/app/hikes`.

**Architecture:** Two `shared.scheduler` jobs replace the manual scripts: a weekly WalkHighlands scrape upserts `hikes.walks`, and a 6-hourly met.no forecast job computes viable hiking windows per walk and stores them as a JSONB column. One SSR-only `/api/hikes/walks` endpoint serializes the whole table; Cloudflare CDN does the viewer fan-out. A SvelteKit route at `/app/hikes` renders a MapLibre map plus client-side filters (duration, distance, ascent, weather windows), porting the filter logic from the old vanilla-JS frontend.

**Tech Stack:** Python 3 / FastAPI / SQLModel / psycopg3 / Postgres (Atlas migrations) / `shared.scheduler` / httpx / SvelteKit (Svelte 5) / MapLibre GL / Bazel + apko.

**Reference implementation:** the `ships` domain is the canonical pattern for every layer (it was the previous standalone-to-monolith migration). When in doubt, copy `projects/monolith/ships/*`, `projects/monolith/frontend/src/routes/public/app/ships/*`, and the ships plan `docs/plans/2026-06-10-ships-into-monolith.md`.

**Non-negotiable house rules (apply to every task):**

- **No em-dashes** in any code, comment, commit, or doc. Use commas, colons, or parentheses.
- **No local test loop.** Write tests, but do NOT run `bazel test`/`pytest`/`vitest` from the workstation. Implement, commit, push the branch, watch CI via `gh pr checks <n> --watch`. Diagnose failures via `mcp__buildbuddy__*`.
- **One code review per PR** at the end, not per task. Implementers self-review before each commit.
- **Conventional Commits** for every commit. Commit frequently (one logical step per commit).
- **Reuse, do not reinvent.** Every pattern below already exists in the monolith; the referenced file is the source of truth for style.

**Worktree:** `/tmp/claude-worktrees/hikes-monolith` on branch `feat/hikes-into-monolith`.

**Ordering note:** this plan lands before the stars migration (`2026-06-13-stars-into-monolith.md`). They are independent PRs; hikes goes first.

---

## Background: what we are deleting, not porting

The old `projects/hikes/` design is shaped by having no server. The following are **deleted, not migrated** (in the follow-up decommission PR, Task 9):

- The **Cloudflare Pages** frontend (`frontend/`, `wrangler.jsonc`, the `jomcgi-hikes` Pages project) and its vanilla-JS/WASM stack.
- The **R2 bundle pipeline**: brotli compression, `bundle.brotli`, the WASM brotli decompressor, the `.brotli` extension content-encoding workaround, and the R2 credentials.
- **SQLite-in-git** (`scrape_walkhighlands/walks.db`, `pydantic-sqlite`) and the manual `bazel run` entry points.
- `requests` + `requests-cache` (the monolith uses httpx; the `no-requests` semgrep rule applies).

What we **keep and port**:

- The WalkHighlands scraping selectors and parsing ladder (`scrape_walkhighlands/scrape.py`): area links from `#choosearea td.cell a`, sub-areas from `#arealist`, walks from `div.walktable > table.table1`, per-walk extraction of name/summary/distance/time/ascent/coords, the uuid5-from-coordinates identity, and the retry/error-collector discipline.
- The met.no forecast fetch and window logic (`update_forecast/update.py`): `locationforecast/2.0/compact`, the User-Agent convention, hourly parsing, viability thresholds (precipitation > 2.0 mm or wind > 80 km/h is not viable), daylight gate (07:00-19:00), 7-day horizon, and the compact window tuple `[timestamp, temp_c, precip_mm, wind_kmh, cloud_pct]`.
- The client-side filter semantics (`frontend/public/app.js`): min/max duration, min/max distance, max ascent, location-radius via haversine, and date-grouped viable windows.

---

## Task 0: Scaffold the `hikes` Python package and register it

**Files:**

- Create: `projects/monolith/hikes/__init__.py`
- Create: `projects/monolith/hikes/router.py` (stub)
- Modify: `projects/monolith/app/main.py` (add `import hikes`; `hikes.register(app)` after `ships.register(app)` at `:220`; `hikes.on_startup_jobs(session)` next to `ships.on_startup_jobs(session)` at `:68`)
- Modify: `projects/monolith/BUILD` (hand-registered tree; copy the `ships` py_library/test blocks)

`__init__.py` mirrors `ships/__init__.py`: `register(app)` includes the router; `on_startup_jobs(session)` registers two jobs:

```python
def on_startup_jobs(session: Session) -> None:
    """Register hikes scheduled jobs (scrape + forecast refresh)."""
    from shared.scheduler import register_job
    from hikes.jobs import refresh_forecasts_handler, scrape_walks_handler

    register_job(
        session,
        name="hikes.scrape_walks",
        interval_secs=7 * 86400,  # weekly; the walk corpus barely changes
        handler=scrape_walks_handler,
        ttl_secs=3 * 3600,  # polite full scrape can take a while
    )
    register_job(
        session,
        name="hikes.refresh_forecasts",
        interval_secs=6 * 3600,
        handler=refresh_forecasts_handler,
        ttl_secs=1800,
    )
```

Commit: `feat(monolith): scaffold hikes package and register router`

---

## Task 1: Postgres schema (migration + SQLModel models)

**Files:**

- Create: `projects/monolith/chart/migrations/20260613000000_hikes_schema.sql`
- Create: `projects/monolith/hikes/models.py`
- Test: `projects/monolith/hikes/models_test.py`

**Design notes:**

- Schema `hikes`. One table; this is deliberately the simplest shape that serves the app.
- `windows` is JSONB holding the compact window tuples exactly as the old bundle format stored them (`[[ts, temp_c, precip_mm, wind_kmh, cloud_pct], ...]`). No join table: the serving endpoint always returns whole walks, and the forecast job always replaces a walk's windows wholesale.
- Walk identity is the existing uuid5-of-coordinates `uuid` (TEXT PK), so re-scrapes upsert cleanly and the seed data (Task 2) stays stable.

```sql
CREATE SCHEMA IF NOT EXISTS hikes;

CREATE TABLE hikes.walks (
    uuid                TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    url                 TEXT NOT NULL,
    distance_km         DOUBLE PRECISION NOT NULL,
    ascent_m            INTEGER NOT NULL,
    duration_h          DOUBLE PRECISION NOT NULL,
    summary             TEXT NOT NULL DEFAULT '',
    latitude            DOUBLE PRECISION NOT NULL,
    longitude           DOUBLE PRECISION NOT NULL,
    windows             JSONB NOT NULL DEFAULT '[]'::jsonb,
    windows_updated_at  TIMESTAMPTZ,
    scraped_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

SQLModel model mirrors it with `__table_args__ = {"schema": "hikes", "extend_existing": True}` (copy `ships/models.py` conventions: tz-aware datetimes, never str timestamps). For the JSONB column use the same JSON column approach the existing models use (check `knowledge/models.py` for the precedent; fall back to `Field(sa_column=Column(JSON))` so SQLite unit tests still work).

Model test: round-trip a walk with windows through a SQLite `create_all` fixture (copy the ships `models_test.py` fixture).

Commit: `feat(monolith): add hikes postgres schema and models`

---

## Task 2: Seed data from walks.db

The corpus in `projects/hikes/scrape_walkhighlands/walks.db` is checked into git and took a full polite scrape to build. Do not make first deploy re-hammer WalkHighlands; seed from the existing data.

**Files:**

- Create: `projects/monolith/chart/migrations/20260613000001_hikes_seed.sql` (generated)
- Create: `projects/monolith/hikes/tools/generate_seed.py` (the generator, kept for provenance)

**Step 1:** Write a small generator that reads `walks.db` (stdlib `sqlite3`, columns `uuid, name, url, distance_km, ascent_m, duration_h, summary, latitude, longitude`) and emits `INSERT INTO hikes.walks (...) VALUES ... ON CONFLICT (uuid) DO NOTHING;` statements with properly escaped literals (double the single quotes; summaries contain apostrophes).

**Step 2:** Run it locally (reading a git file is fine, this is not a test run), review the output size (1,620 rows; walks.db also has a stale viable_dates column, ignore it), and commit the generated SQL as the migration. Atlas applies it after the schema migration by timestamp ordering.

**Step 3:** Sanity-check a couple of rows against the old frontend's data.

Commit: `feat(monolith): seed hikes.walks from legacy walks.db`

---

## Task 3: Port the WalkHighlands scraper as a pure module + scheduled job

**Files:**

- Create: `projects/monolith/hikes/walkhighlands.py` (parsing, pure where possible)
- Create: `projects/monolith/hikes/jobs.py` (`scrape_walks_handler`)
- Test: `projects/monolith/hikes/walkhighlands_test.py` (build small HTML fixtures per selector; port any existing test cases from `projects/hikes/`)

**Design:**

- Port the four scraping stages from `projects/hikes/scrape_walkhighlands/scrape.py`, but split fetch from parse: each `parse_*(html: str)` function takes HTML and returns links/data (unit-testable, no network), and a thin async fetch layer uses `httpx.AsyncClient` with a shared client, 15s timeouts, and a bounded semaphore (concurrency 2-4 plus a small delay; be polite, this is someone else's site).
- Keep the `Walk` pydantic shape, `parse_duration`, the `TimeLength` handling, and the uuid5 identity exactly as-is. Add `@pip//` deps to the monolith `py_library` in `BUILD` (beautifulsoup4, timelength, pydantic-extra-types) as needed.
- Drop `requests`/`requests-cache`/`pydantic-sqlite`; replace the `print()` calls with `logger` calls (monolith logging is structured via OTEL).
- The handler upserts results into `hikes.walks` (`ON CONFLICT (uuid) DO UPDATE` on the scraped columns only; never touch `windows`/`windows_updated_at`), updating `scraped_at`. Partial scrape failures keep the ErrorCollector spirit: log a summary, upsert what succeeded, never raise out of the handler.
- Do the network phase before any session use (mirror how `ships.ingest` separates network work from `Session` usage, and how home's calendar poll handler is shaped).

Commit: `feat(monolith): port walkhighlands scraper as scheduled job`

---

## Task 4: Forecast refresh job (met.no windows)

**Files:**

- Create: `projects/monolith/hikes/forecast.py` (fetch + window computation, pure logic separated)
- Modify: `projects/monolith/hikes/jobs.py` (`refresh_forecasts_handler`)
- Test: `projects/monolith/hikes/forecast_test.py`

**Design (port from `projects/hikes/update_forecast/update.py`):**

- `httpx.AsyncClient` against `https://api.met.no/weatherapi/locationforecast/2.0/compact`, params `lat`/`lon` rounded to 4 dp, User-Agent `"jomcgi.dev/app/hikes (https://github.com/jomcgi/homelab)"`. Bounded concurrency around 10 with a rate cap (met.no asks for max 20 req/s; the old code used 20 workers, stay at or below). The corpus is 1,620 walks, so a run takes roughly 90 seconds at the rate cap.
- Pure `compute_windows(hourly, now)` ports the exact filter ladder: skip past hours, skip beyond 7 days, skip outside 07:00-19:00, skip precip > 2.0 mm, skip wind > 80 km/h; emit `[timestamp, temp_c, precip_mm, wind_kmh, cloud_pct]` with the same rounding rules.
- Handler: load all walks (uuid, lat, lon), fetch forecasts, compute windows, then batch-update `windows` + `windows_updated_at` in one transaction. A walk whose fetch failed keeps its previous windows (stale beats empty).

**Tests:** port the threshold edge cases (precip exactly 2.0 viable, above not; wind 80 km/h viable, above not; daylight bounds; past/expired hours dropped; null temp/cloud defaults).

Commit: `feat(monolith): met.no forecast windows job for hikes`

---

## Task 5: `/api/hikes/walks` endpoint (SSR-only, CDN-cached)

**Files:**

- Modify: `projects/monolith/hikes/router.py`
- Test: `projects/monolith/hikes/router_test.py`

**Design:** clone `ships/router.py` `get_snapshot` wholesale: module cache-control constant, ETag from `(row_count, max(windows_updated_at))`, conditional GET 304, `_as_utc` helper. Payload is `{count, generated_at, walks: [...]}` with all columns. Cache header: `public, s-maxage=1800, stale-while-revalidate=3600, stale-if-error=86400` (forecasts refresh 6-hourly; 30 min freshness is plenty).

**Critical:** do **not** add `/api/hikes/*` to `chart/templates/httproute-public.yaml`. SSR reaches it at `http://localhost:8000`; the page is the public surface (same rule as ships).

**Tests:** seed walks via SQLite fixture + `TestClient`; assert payload, Cache-Control, ETag, and 304 on `If-None-Match`.

Commit: `feat(monolith): hikes walks endpoint (SSR-only, CDN-cached)`

---

## Task 6: Frontend route `/app/hikes` (map + filters, neobrutalist)

**Files:**

- Create: `projects/monolith/frontend/src/routes/public/app/hikes/+page.server.js` (clone of ships `+page.server.js`: fetch `/api/hikes/walks`, forward ETag, set `HIKES_WALKS_CACHE_CONTROL`)
- Create: `projects/monolith/frontend/src/routes/public/app/hikes/+page.js` (`export const ssr = false;` MapLibre needs window; data is still SSR-sourced via the server load)
- Create: `projects/monolith/frontend/src/routes/public/app/hikes/+page.svelte`
- Create: `projects/monolith/frontend/src/lib/public/components/hikes/HikesMap.svelte`
- Create: `projects/monolith/frontend/src/lib/public/hikes/filters.js` (pure, ported from `app.js`)
- Modify: `projects/monolith/frontend/src/lib/cache-headers.js` (add `HIKES_WALKS_CACHE_CONTROL`, keep-in-sync comment mirroring the python constant)
- Tests: `page.server.test.js` (clone ships'), `filters.test.js`
- Modify: `projects/monolith/frontend/BUILD` only if new globs are needed (maplibre-gl is already a dependency since ships)

**Design:**

- **Filters module (pure, tested):** port from `projects/hikes/frontend/public/app.js`: `filterWalksByCharacteristics(walks, {minDuration, maxDuration, minDistance, maxDistance, maxAscent})`, `filterWalksByLocation(walks, lat, lon, radiusKm)` with the haversine helper, and window grouping by date (UK timezone day-bucketing, as the old code did) plus a "viable on date X / viable in next N days" predicate.
- **Map:** reuse the `ShipsMap.svelte` approach (OpenFreeMap liberty style, palette restyle, design-system tokens). Walk markers; clicking opens a hard-shadow card with name, stats, summary, next viable windows table, and the WalkHighlands link.
- **Chrome:** neobrutalist via `design-system.css` tokens only (no hardcoded colors): header bar "HIKES · n walks · m viable today", a filter sidebar with the five numeric filters plus a date strip of upcoming viable days, and a list view fallback under the map. Stay inside the existing token set so `/app/hikes` and `/app/ships` read as siblings.
- Snapshot refresh: `setInterval(invalidateAll, 30 * 60_000)` cleared on destroy (windows only change 6-hourly).

Commit: `feat(frontend): add /app/hikes walk planner with map and filters`

> macOS gotcha (from the ships plan): do not run `pnpm build`; it can clobber the Bazel `BUILD` file via the case-insensitive `build/` dir. Let CI build.

---

## Task 7: Chart bump

No new secrets (WalkHighlands and met.no need none) and no new env vars. The only chart change is the two migration files, which require a release:

- Modify: `projects/monolith/chart/Chart.yaml` (version bump)
- Modify: `projects/monolith/deploy/application.yaml` (`targetRevision` to match; both files in the same commit, per house rule)

Render check: `helm template monolith projects/monolith/chart/ -f projects/monolith/deploy/values.yaml > /dev/null`.

Commit: `feat(monolith): bump chart for hikes schema migrations`

---

## Task 8: Push, watch CI, end-of-PR review, verify live

1. Push `feat/hikes-into-monolith`, open the PR, `gh pr checks <n> --watch`. Diagnose failures via `mcp__buildbuddy__get_invocation` (commitSha selector) then `get_target`/`get_log`; quote the actual error before hypothesizing.
2. One comprehensive code review of the full diff; address findings.
3. Merge with `gh pr merge --rebase`.
4. Verify live after ArgoCD syncs and the Atlas migration applies:
   - The page renders the seeded corpus: `curl -s https://jomcgi.dev/app/hikes` shows walks.
   - Trigger `hikes.refresh_forecasts` once via the scheduler skill and confirm `windows_updated_at` populates and viable windows appear on the page.
   - `curl -I https://jomcgi.dev/app/hikes` shows the Cache-Control; `/api/hikes/walks` is NOT reachable from the public internet.
   - Confirm `hikes.scrape_walks` appears in the jobs table with the weekly interval (do not force a full scrape just to test; the seed already proves the write path).

---

## Task 9 (follow-up PR, AFTER live verification): decommission standalone hikes

Only once `/app/hikes` is verified serving walks with fresh windows:

- Delete: `projects/hikes/` entirely (scrapers, walks.db, frontend, wrangler config, Playwright tests).
- Run `format`; confirm no `//projects/hikes/...` Bazel targets remain referenced.
- Tear down the Cloudflare side manually (dashboard or wrangler): the `jomcgi-hikes` Pages project, the `jomcgi-hikes` R2 bucket, the R2 API token, and the `hike-assets.jomcgi.dev` custom domain. Add a redirect from the old Pages URL to `https://jomcgi.dev/app/hikes` if the old URL ever circulated.
- Commit: `chore(hikes): decommission standalone hikes app, now served in monolith`.

---

## Open follow-ups (not blocking)

- Bog factor and max-elevation filters (from the walk.jomcgi.dev knowledge note) need data the scraper does not yet extract (terrain notes, summit height); candidate second iteration.
- A SigNoz HTTP check + alert for `/app/hikes` via the `add-httpcheck-alert` skill.
- Walk detail pages (`/app/hikes/[uuid]`) with elevation profiles if we ever scrape them.
