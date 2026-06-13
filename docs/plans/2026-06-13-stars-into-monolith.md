# Stars into Monolith Implementation Plan (self-contained, typed storage)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

> **Supersedes** the earlier "Option A: hybrid" version of this file. The hybrid
> approach (keep the stargazer CronJob + NGINX alive, pull `/best` JSON over the
> cluster network) was rejected. The monolith is now the source of truth: it
> fetches MET Norway itself, scores it, and owns typed rows in Postgres. The
> stargazer geospatial pipeline is not a runtime dependency and is retired in a
> later, separate PR.

**Goal:** Serve a neobrutalist dark-sky map at `jomcgi.dev/app/stars` from a self-contained `stars` domain in the monolith, with an explicit per-hour TTL so it never serves a forecast window that has already elapsed.

**Architecture:** The monolith fetches MET Norway forecasts directly for a curated seed list of ~30 Scottish dark-sky sites, scores each dark hour for astronomy suitability, and stores **all** future qualifying hours in a typed `stars.site_hours` table (one row per site-hour; static site metadata stays in `seed.py` and is joined in at read time). Freshness has two layers sharing one cutoff (the top of the current clock hour): an **hourly prune job** (`DELETE WHERE hour_time < top_of_hour()`, indexed housekeeping) and a **read-time filter** in the endpoint (`WHERE hour_time >= top_of_hour()`, the correctness backstop) that also folds the current hour into the ETag so the CDN turns over hourly. The read endpoint is SSR-only and CDN-cached per ADR 002.

**Tech Stack:** Python 3 / FastAPI / SQLModel / psycopg3 / Postgres (Atlas migrations) / `shared.scheduler` / `httpx` / `astral` / pydantic / SvelteKit (Svelte 5) / MapLibre GL / Bazel + apko.

**Reference implementations:**

- hikes: `projects/monolith/hikes/{models,jobs,router,forecast,__init__}.py`, migration `chart/migrations/20260613000000_hikes_schema.sql`, frontend `frontend/src/routes/public/app/hikes/*`.
- ships: `projects/monolith/ships/router.py` (ETag + conditional-GET shape), `ShipsMap.svelte` (MapLibre).
- scheduler: `projects/monolith/shared/scheduler.py`: `register_job(session, *, name, interval_secs, handler, ttl_secs=1200)`. Fixed-interval (not cron); a handler may return a datetime to override `next_run_at`; handlers may be sync (pure DB) or async (network).
- Reusable verbatim from `origin/feat/stars-domain` (`git show <ref>:<path>`): `stars/scoring.py` + `scoring_test.py` (pure, stargazer parity) and `stars/seed.py` (the 30 sites). Strip em-dashes from copied docstrings. Discard that branch's `models.py`, `router.py`, `service.py` storage, and its `refresh_runs` migration; reuse only the fetch + score logic.

**Non-negotiable house rules (every task):**

- **No em-dashes** anywhere (code, comments, commits, docs). Commas, colons, parentheses, or split the sentence.
- **No local test loop:** no `bazel test` / `pytest` / `vitest` / `pnpm build` from the workstation. Implement, commit, push, watch CI via `gh pr checks <n> --watch`; diagnose via `mcp__buildbuddy__*`, quoting the actual error first. `helm template` render is allowed (not a test).
- **One code review per PR** at the end, not per task. Self-review before each commit.
- **Conventional Commits**, one logical step per commit.
- New monolith test/binary targets are **hand-registered** in `projects/monolith/BUILD` (gazelle will not add them; copy the `hikes`/`ships` blocks).
- No hardcoded `.svc.cluster.local` URLs in code defaults; no manual `@sha256:` digests.
- Test fixtures use SQLite + `create_all` (not migrations); mirror any CHECK constraints in `__table_args__`.
- Chart bumps touch `chart/Chart.yaml` version AND `deploy/application.yaml` `targetRevision` in the same commit.

**Worktree:** `/tmp/claude-worktrees/stars-monolith` on branch `feat/stars-into-monolith` (created off `origin/main`). Note: main HEAD already has an "APPS" dropdown in the public nav for `/app/*`; add a `/app/stars` entry to it.

---

## Data contract

MET Norway `locationforecast/2.0/complete` returns `properties.timeseries[]`, each with `data.instant.details` (`cloud_area_fraction`, `relative_humidity`, `fog_area_fraction`, `wind_speed`, `air_temperature`, `dew_point_temperature`, `air_pressure_at_sea_level`) and `data.next_1_hours.summary.symbol_code`. An hour qualifies if the sun is at/below nautical twilight (astral elevation <= -12 deg) AND `score >= STARS_MIN_DISPLAY_SCORE` (default 60). `dew_spread = air_temperature - dew_point_temperature`. **Store every qualifying future hour** (not a global top-N; that breaks pruning). MET requires a descriptive User-Agent with contact info (generic UA returns 403); mirror hikes. Egress to `api.met.no` is already proven by hikes, so no new NetworkPolicy / Linkerd allowance is needed.

## Schema

```sql
CREATE SCHEMA IF NOT EXISTS stars;
CREATE TABLE stars.site_hours (
    site_id     TEXT NOT NULL,
    hour_time   TIMESTAMPTZ NOT NULL,
    score       DOUBLE PRECISION NOT NULL,
    cloud_area_fraction DOUBLE PRECISION NOT NULL,
    relative_humidity   DOUBLE PRECISION NOT NULL,
    wind_speed          DOUBLE PRECISION NOT NULL,
    air_temperature     DOUBLE PRECISION NOT NULL,
    dew_spread          DOUBLE PRECISION NOT NULL,
    symbol      TEXT NOT NULL DEFAULT '',
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, hour_time)
);
CREATE INDEX idx_stars_site_hours_time  ON stars.site_hours (hour_time);
CREATE INDEX idx_stars_site_hours_score ON stars.site_hours (score DESC);
```

Migration filename `chart/migrations/20260613000010_stars_schema.sql` (sorts after hikes). Do not hand-edit `atlas.sum`.

---

## Task 0: Shared top-of-hour cutoff

**Files:** create `projects/monolith/shared/forecast_freshness.py` + `_test.py`; register lib + test in `projects/monolith/BUILD`.
`top_of_hour(now: datetime | None = None) -> datetime` returns the start of the current UTC clock hour. Tests: truncation, default-now path. (This is the single shared cutoff stars uses now and the future hikes-typed PR will reuse.)
Commit: `feat(monolith): shared top-of-hour forecast TTL cutoff`

## Task 1: Scaffold the stars package and register jobs

**Files:** create `stars/__init__.py`, `stars/router.py` (stub); modify `app/main.py` (`import stars`; `stars.register(app)` ~`:222`; `stars.on_startup_jobs(session)` ~`:69`); modify `projects/monolith/BUILD` (copy the hikes block for `stars/**/*.py`).
`on_startup_jobs` registers `stars.refresh` (`interval_secs=3*3600`, `ttl_secs=900`, async `refresh_handler`) and `stars.prune_hours` (`interval_secs=3600`, `ttl_secs=300`, sync `prune_hours_handler`).
Commit: `feat(monolith): scaffold stars package and register jobs`

## Task 2: Schema migration + SQLModel model

**Files:** create the migration above; create `stars/models.py` (`SiteHour`, `schema="stars"`, `extend_existing`, tz-aware datetimes, PK `(site_id, hour_time)`); test `stars/models_test.py` (SQLite `create_all` round-trip); register in BUILD.
Commit: `feat(monolith): add stars site_hours schema and model`

## Task 3: Port scoring + seed

**Files:** copy `stars/scoring.py`, `stars/scoring_test.py`, `stars/seed.py` verbatim from `origin/feat/stars-domain` (strip em-dashes); add `stars/seed_test.py` (unique ids, lat in [54,61], lon in [-8,0], non-empty `lp_zone`); register tests in BUILD.
Commit: `feat(monolith): port stars scoring and dark-sky seed list`

## Task 4: Forecast fetch + refresh job

**Files:** create `stars/forecast.py` (bounded-concurrency MET fetch with UA + `sleep(1/rate)`; `score_location` keeps ALL qualifying hours sorted by time) and `stars/jobs.py` `refresh_handler` (network first with no session held; per successfully fetched site delete its rows and insert new scored hours with a shared `fetched_at`; failed fetches leave existing rows, stale beats empty; fully empty result is a logged no-op); tests `forecast_test.py` (astral/httpx mocked) + `jobs_test.py` (monkeypatch `fetch_all`); register in BUILD.
Commit: `feat(monolith): stars refresh job fetching and scoring met.no`

## Task 5: Hourly prune job

**Files:** add `prune_hours_handler` to `stars/jobs.py`: `session.exec(delete(SiteHour).where(SiteHour.hour_time < top_of_hour()))`, commit only if rows deleted, do not touch `fetched_at`; test in `jobs_test.py` with rows straddling the current hour.
Commit: `feat(monolith): hourly prune of elapsed stars hours`

## Task 6: /api/stars/sites endpoint

**Files:** flesh out `stars/router.py`; test `stars/router_test.py`.
Select rows `WHERE hour_time >= top_of_hour(now)`; group by `site_id`; join static metadata from `seed.py`; per site `best_score = max(score)`, `best_hours = top-8 by score`; drop sites with no future hours; sort sites by `best_score` desc. ETag `f'"v1-{top_of_hour(now).isoformat()}-{max_fetched}-{count}"'`; honor `If-None-Match` -> 304. `Cache-Control: public, max-age=0, s-maxage=1800, stale-while-revalidate=3600, stale-if-error=86400`. CRITICAL: do NOT add `/api/stars/*` to `chart/templates/httproute-public.yaml` (SSR-only at `http://localhost:8000`). Tests: ordering, past-hours-omitted, header present, 304 on ETag, empty-table shape.
Commit: `feat(monolith): stars sites endpoint with per-hour read filter`

## Task 7: Frontend /app/stars

**Files:** create `frontend/src/routes/public/app/stars/{+page.server.js,+page.js,+page.svelte}` and `frontend/src/lib/public/components/stars/StarsMap.svelte` (clone hikes route + ShipsMap, DARK basemap variant, MapLibre centered on Scotland ~`[-4.2,57.0]` zoom 6, markers by `best_score` bucket, hard-shadow site panel with best-hours table + "open in maps" link, header with `fetched_at` age, honest empty state, `setInterval(invalidateAll, 30*60000)` + `nowMs` tick cleared on destroy); modify `frontend/src/lib/cache-headers.js` (add `STARS_SITES_CACHE_CONTROL`, keep-in-sync comment); add a `/app/stars` entry to the public nav APPS dropdown (grep `app/hikes|app/ships` under `frontend/src`); test `page.server.test.js` (clone hikes). Do NOT run `pnpm build`.
Commit: `feat(frontend): add /app/stars dark-sky map`

## Task 8: Chart wiring + version bump

**Files:** modify `chart/templates/deployment.yaml` (`STARS_USER_AGENT` + optional `STARS_RATE_LIMIT`, `STARS_MIN_DISPLAY_SCORE` from values), `chart/values.yaml` (defaults; real UA contact), bump `chart/Chart.yaml` + `deploy/application.yaml` `targetRevision` same commit. Render-check with `helm template`.
Commit: `feat(monolith): wire stars env vars and bump chart`

## Task 9: PR push, CI, review, merge, verify

Push, open PR, `gh pr checks <n> --watch`, diagnose via `mcp__buildbuddy__*` (commitSha selector -> get_target -> get_log). One comprehensive code review of the full diff. Merge `--rebase`. Verify live: trigger `stars.refresh` (table populates, log shows MET fetch); `curl -sI https://jomcgi.dev/app/stars` shows Cache-Control and renders; `/api/stars/sites` not publicly reachable; trigger `stars.prune_hours`.

## Out of scope

- Hikes TTL / typed-storage normalization (owner does it in a separate PR; reuses `top_of_hour`).
- Retiring the standalone stargazer pipeline (separate follow-up after `/app/stars` is verified live).
