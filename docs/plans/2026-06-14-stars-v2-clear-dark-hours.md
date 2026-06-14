# Stars v2: Clear-Dark-Hours Metric + Dark-Sky Grid Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the continuous `Q = D×C×W` stargazing metric with a concrete, honest "clear dark hours" count (hours where the sun is below −12° AND cloud cover is under 10%), and rebuild the site grid so it actually lands on dark, road-accessible spots instead of the inverted set it produces today.

**Architecture:** Two independent reworks that meet at the grid: (1) a metric swap that ripples through `scoring.py` → `site_hours`/accumulator schema → forecast scoring → prune banking → read endpoint → frontend; (2) an offline grid-v2 generator that meshes Scotland land, keeps points within 2 km of an OSM road (Geofabrik), then aggressively keeps only the darkest by World Atlas 2015 (Falchi) sky brightness. Because the grid's `site_id`s change and the accumulator columns change, the `stars.*` history tables are reset and re-backfilled.

**Tech Stack:** Python (SQLModel, FastAPI), Postgres (Atlas migrations), SvelteKit + MapLibre, offline geospatial (rasterio, geopandas, shapely 2) for grid-gen. Tests on BuildBuddy CI (no local test loop — implement, commit, push, watch CI).

**Key contracts (decided up front):**

- A **clear-dark hour** = `sun_elevation_deg < -12.0` AND `cloud_area_fraction < 10.0`.
- A **dark hour** (denominator) = `sun_elevation_deg < -12.0`.
- Accumulator sufficient stats per `(site_id, month)`: `dark_hours` (count), `clear_dark_hours` (count). Headline metric = `clear_dark_hours`. Clarity rate = `clear_dark_hours / dark_hours`.
- Light pollution → **local `scotland_lp_2024.tif`** from the stargazer PVC (`kubectl cp -n stargazer <api-pod>:/data/processed/scotland_lp_2024.tif -c api ...`), 2024 data, EPSG:4326, ~900 m px. It is a 3-band RGB render using a **discrete 8-stop legend** (validated lossless: median nearest-color distance 0.0). Classify each pixel by nearest match to these swatches, darkest→brightest: `black(0,0,0) pristine`, `gray(66,66,66) excellent`, `blue(33,84,216) rural`, `green(31,161,42)`, `yellow(184,166,37)`, `orange(253,150,80)`, `pink(251,153,138)`, `white(242,242,242)`. **Use float/int32 distance (uint8 squared overflows int16).** Keep the **darkest zones (black + gray + blue)** = the user's "excellent and better" cutoff (verified: Cairngorms/Assynt=gray, Galloway=blue dark-sky-park; cities=pink/orange). NOTE the `color_palette.json` on the PVC is a mismatched legend, ignore it; the colorbar swatches above are authoritative. (World Atlas 2015 download is the fallback only if this raster is unavailable.)
- Roads → **local, no download**: copy `scotland-roads.geojson` (193 MB) from the stargazer PVC (`kubectl cp -n stargazer <api-pod>:/data/processed/scotland-roads.geojson -c api ...`); reproject to EPSG:27700; distance test via `STRtree.query(points, predicate="dwithin", distance=2000)`. (Fallback if it's gone: Geofabrik `scotland-latest-free.shp.zip` → `gis_osm_roads_free_1.shp`.)
- Altitude (optional) → SRTM tiles on the stargazer PVC (`/data/raw/srtm_tiles/`) can populate real `altitude_m` (currently hardcoded 0).
- Grid mesh spacing: **2 km**; aggressive dark filter = keep points with World Atlas value below the **excellent** cutoff (~0.014 mcd/m², ratio < 0.08 of the 0.174 natural reference) AND within the **darkest 25%** of road-accessible candidates (whichever is stricter), targeting ~150–400 genuinely-dark accessible sites.

**Workflow note (repo-specific, overrides the skill's TDD loop):** There is **no local test loop** (Mac runners aren't in the BuildBuddy `workflows` pool). For each task: write the test AND the implementation together, self-review, commit. **Defer all test execution to end-of-plan CI** on the pushed branch. The "run pytest, expected FAIL/PASS" sub-steps below describe intent for the reader; do not run them locally. The offline grid-gen + backfill (Tasks 8–9) are run by hand on the workstation, not in CI.

**Out of scope / explicitly dropped:** the `Q` continuous model, `darkness_factor`/`cloud_factor`/`weather_modifier`/`quality_score`/`calculate_astronomy_score`, and the `sum_q`/`sum_darkness`/`sum_clarity` columns. The live "best upcoming hours" list stays but is filtered/ranked by clear-dark instead of Q.

---

## Task 1: Replace the scoring core with the clear-dark predicate

**Files:**

- Modify: `projects/monolith/stars/scoring.py`
- Test: `projects/monolith/stars/scoring_test.py`

**What:** Strip the Q machinery. Keep `WeatherData` and the raw helpers only if still referenced; otherwise remove. Add the predicate + thresholds.

```python
# scoring.py — new core
NAUTICAL_DARK_DEG = -12.0   # sun below this = "dark" for the clear-dark metric
CLEAR_CLOUD_MAX_PCT = 10.0  # cloud cover (%) strictly below this = "clear"


def is_dark_hour(sun_elevation_deg: float) -> bool:
    """True when the sun is far enough below the horizon to count as a dark hour
    (nautical/astronomical), the denominator for the clear-dark rate."""
    return sun_elevation_deg < NAUTICAL_DARK_DEG


def is_clear_dark_hour(sun_elevation_deg: float, cloud_area_fraction: float) -> bool:
    """The stars v2 unit of value: a dark hour (sun < -12 deg) that is also clear
    (cloud < 10%). Counted per site per month-of-year (live + ERA5 climatology)."""
    return is_dark_hour(sun_elevation_deg) and cloud_area_fraction < CLEAR_CLOUD_MAX_PCT
```

**Steps:**

1. Write `scoring_test.py` cases: boundary at exactly −12.0 (not dark), −12.01 (dark); cloud exactly 10.0 (not clear), 9.99 (clear); combined truth table.
2. Implement the two predicates; delete `darkness_factor`, `cloud_factor`, `weather_modifier`, `quality_score`, `calculate_astronomy_score`, `is_dark_enough`, and the `_humidity/_fog/_wind/_dew_score` helpers unless a non-stars caller imports them (grep first: `grep -rn "from stars.scoring\|stars\.scoring\." projects/monolith --include=*.py`).
3. Keep `WeatherData` only if `forecast.py` still constructs it; otherwise inline the fields it needs.
4. Commit: `refactor(monolith): replace stars Q score with clear-dark predicate`.

---

## Task 2: Migrate `site_hours` (drop Q residue)

**Files:**

- Create: `projects/monolith/chart/migrations/20260614000010_stars_v2_site_hours.sql`
- Modify: `projects/monolith/stars/models.py` (`SiteHour`)
- Test: `projects/monolith/stars/models_test.py`

**What:** `SiteHour` already has `sun_elevation_deg` and `cloud_area_fraction` (the only inputs the new metric needs). Drop the Q-derived columns.

```sql
-- 20260614000010_stars_v2_site_hours.sql
ALTER TABLE stars.site_hours DROP COLUMN IF EXISTS score;
ALTER TABLE stars.site_hours DROP COLUMN IF EXISTS darkness_factor;
ALTER TABLE stars.site_hours DROP COLUMN IF EXISTS cloud_factor;
```

Remove those three fields from the `SiteHour` SQLModel. Update `models_test.py` to stop asserting them and to round-trip `sun_elevation_deg` + `cloud_area_fraction`.

Commit: `feat(monolith): drop Q columns from stars.site_hours (v2 metric)`.

---

## Task 3: Re-shape the accumulator tables to clear-dark counts

**Files:**

- Create: `projects/monolith/chart/migrations/20260614000020_stars_v2_accumulators.sql`
- Modify: `projects/monolith/stars/models.py` (`SiteMonthStat`, `SiteMonthClimatology`)
- Test: `projects/monolith/stars/models_test.py`

**What:** Replace `window_count, sum_q, sum_darkness, sum_clarity` with `dark_hours, clear_dark_hours` on BOTH tables. Data is reset (new grid + new metric), so drop-and-recreate the metric columns.

```sql
-- 20260614000020_stars_v2_accumulators.sql
-- Wipe v1 metric data; the grid site_ids change in v2 so the old rows are stale.
TRUNCATE stars.site_month_stats;
TRUNCATE stars.site_month_climatology;

ALTER TABLE stars.site_month_stats   DROP COLUMN window_count, DROP COLUMN sum_q,
    DROP COLUMN sum_darkness, DROP COLUMN sum_clarity;
ALTER TABLE stars.site_month_stats   ADD COLUMN dark_hours INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN clear_dark_hours INTEGER NOT NULL DEFAULT 0;

ALTER TABLE stars.site_month_climatology DROP COLUMN window_count, DROP COLUMN sum_q,
    DROP COLUMN sum_darkness, DROP COLUMN sum_clarity;
ALTER TABLE stars.site_month_climatology ADD COLUMN dark_hours INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN clear_dark_hours INTEGER NOT NULL DEFAULT 0;
```

Update both SQLModels to `dark_hours: int`, `clear_dark_hours: int` (drop the four old fields). Update `models_test.py`. Commit: `feat(monolith): clear-dark accumulator columns (v2)`.

---

## Task 4: Forecast scoring emits clear-dark hours

**Files:**

- Modify: `projects/monolith/stars/forecast.py` (`score_location`)
- Test: `projects/monolith/stars/forecast_test.py`

**What:** Keep every **dark** hour (sun < −12°), tag each with `is_clear` and the raw `cloud_area_fraction`/`sun_elevation_deg`. Drop `score`/`darkness_factor`/`cloud_factor` from the emitted dict. The "best upcoming hours" list is the clear dark hours, ordered by time. Site-level live value = count of upcoming clear-dark hours.

- Replace the `darkness_factor(e) <= 0` gate with `if not is_dark_hour(e): continue`.
- Replace the `quality_score`/`q <= 0` filter with: still keep the hour if dark; set `is_clear = cloud < 10`.
- Emit per hour: `time, sun_elevation_deg, cloud_area_fraction, air_temperature, dew_spread, symbol, is_clear`.
- Update `forecast_test.py` accordingly.

Commit: `feat(monolith): forecast emits clear-dark hours (v2)`.

---

## Task 5: Prune job banks dark_hours + clear_dark_hours

**Files:**

- Modify: `projects/monolith/stars/jobs.py` (`_prune_elapsed`)
- Test: `projects/monolith/stars/jobs_test.py`

**What:** For each elapsing `site_hours` row, bucket by `(site_id, hour_time.month)` and increment `dark_hours += 1` (every elapsing row is already a dark hour after Task 4) and `clear_dark_hours += 1` when `is_clear_dark_hour(sun_elevation_deg, cloud_area_fraction)`. Keep the exactly-once semantics (sole elapsed-remover; `synchronize_session=False` delete). Build new rows and mutate existing via `session.get(SiteMonthStat, (site_id, month))`; `add_all` once.

Update `jobs_test.py`: seed elapsed hours with mixed sun/cloud, assert the two counts.

Commit: `feat(monolith): prune banks clear-dark counts (v2)`.

---

## Task 6: Read path returns clear-dark hours

**Files:**

- Modify: `projects/monolith/stars/router.py` (`get_history`, `get_sites` if it surfaces Q), `projects/monolith/stars/grid.py` (`_load_climatology_sync`)
- Test: `projects/monolith/stars/router_test.py`, `projects/monolith/stars/climatology_test.py`

**What:**

- `get_history`: keep the all-year (`month=0`) full-outer-join, but sum `dark_hours` + `clear_dark_hours` per site. Per-site payload: `id, name, lat, lon, clear_dark_hours, dark_hours, clear_rate (= clear/dark, guard /0), months: {1..12: clear_dark_hours}` for the per-site graph. Order by `clear_dark_hours` desc. ETag folds `month, len, max clear_dark_hours, total dark_hours`.
- The `months` map requires per-month data even in the all-year view: build a `{site_id: {month: clear_dark_hours}}` index from the unfiltered query so the frontend graph has 12 bars regardless of the selected view.
- `_load_climatology_sync`: parse `dark_hours`/`clear_dark_hours` from `climatology.json` instead of the sum\_\* fields.
- Update both test files (the `_seed_stat`/`_seed_climo` helpers take the new columns).

Commit: `feat(monolith): /history returns clear-dark hours + per-month graph data (v2)`.

---

## Task 7: Frontend — color by clear-dark hours + per-site monthly graph

**Files:**

- Modify: `projects/monolith/frontend/src/lib/public/components/stars/StarsMap.svelte`
- Modify: `projects/monolith/frontend/src/routes/public/app/stars/+page.svelte`
- Modify: `projects/monolith/frontend/src/lib/public/stars/heat.js` (helpers/labels) + `heat.test.js`

**What:**

- Cell fill `score` = the site's `clear_dark_hours` (per the selected month, or all-year sum). Keep the per-view relative normalization (`relativeMax`) since counts vary widely between months. Legend relabels to "Clear dark hours" Low/Med/High.
- Replace the historical card body (which showed realized-quality/darkness/clarity) with: headline `clear_dark_hours`, `dark_hours`, `clear_rate` %, and a **12-month bar chart** of `months[1..12]` (inline SVG bars, no new dep; mirror the neobrutalist style). This is the "graph" the user asked for.
- Live card: list upcoming clear-dark hours (time, cloud %, temp, sky) instead of scored hours.
- Update `heat.test.js` if helper signatures change.

Commit: `feat(monolith): clear-dark heatmap + per-site monthly graph (v2)`.

---

## Task 8: Offline grid-v2 generator (mesh → road ≤2 km → aggressive dark)

**Files:**

- Create: `projects/monolith/stars/grid_gen/generate_grid_v2.py`
- Test: `projects/monolith/stars/grid_gen/generate_grid_v2_test.py` (pure helpers only)
- Modify: `projects/monolith/BUILD` (add a `stars_generate_grid_v2_test` target mirroring `stars_generate_grid_test`; grid_gen stays excluded from the runtime image)

**What:** A standalone offline script (geospatial deps allowed; NOT in the image). Pipeline:

1. Build the Scotland-land mesh at **2 km** spacing (reuse the existing land/Scotland point-in-polygon from `generate_grid.py`, or import its helpers).
2. **Road filter:** load `gis_osm_roads_free_1.shp`, reproject roads + mesh points to EPSG:27700, `STRtree.query(points, predicate="dwithin", distance=2000)`; keep points within 2 km of a road. Optionally drop `fclass in {path, footway, steps, cycleway, bridleway}` so "road" means drivable.
3. **Dark filter:** open `scotland_lp_2024.tif` (GDAL), read the 3 RGB bands, classify each mesh point's pixel by **nearest match to the 8 legend swatches** (use float distance, NOT int16). Keep points whose zone is in `{black, gray, blue}` (pristine/excellent/rural). The zones are exact (median distance 0.0), so no percentile needed; if the kept count is too high, tighten to `{black, gray}`.
4. Emit `grid.json`: `{id: "scotland-NNNN", name, lat, lon, altitude_m, lp_zone}` where `lp_zone` is the classified zone name (`pristine/excellent/rural`). `altitude_m` from SRTM tiles if wired (else 0). Keep ids stable-sorted by (lat, lon).

**Testable pure helpers** (unit-tested without the big files): `classify_zone(rgb) -> zone` (nearest-swatch), `is_dark_zone(zone) -> bool`, the mesh generator. Mock/skip the raster + roads I/O.

Document the run in the module docstring (kubectl cp commands for the PVC raster + roads, run command, expected count).

Commit: `feat(monolith): stars grid-v2 generator (road + LP-zone dark filter)`.

---

## Task 9: Backfill-v2 counts clear-dark hours from ERA5

**Files:**

- Modify: `projects/monolith/stars/grid_gen/backfill_climatology.py`

**What:** Replace the Q aggregation with clear-dark counting. Reuse the existing resumable fetch + adaptive 429 backoff + NOAA `sun_elevation_deg`. Per dark hour (`sun < -12`): `dark_hours += 1`; if `cloud_cover[i] < 10`: `clear_dark_hours += 1`. Emit rows `{site_id, month, dark_hours, clear_dark_hours}`. Keep the NOAA sun-elevation + threshold logic in sync with `scoring.py` (KEEP IN SYNC comment).

Commit: `fix(monolith): backfill counts clear-dark hours (v2)`.

---

## Task 10: Chart bump + deploy + regenerate data (operational)

**Files:**

- Modify: `projects/monolith/chart/Chart.yaml` (version), `projects/monolith/deploy/application.yaml` (`targetRevision`)

**What & order:**

1. Bump chart (e.g. `0.132.x → 0.133.0`), keep `Chart.yaml` + `application.yaml` in sync. Commit, push, open PR, watch CI green, merge (rebase), verify rollout (new pod + `/api/stars/history` returns the new fields).
2. **Offline (workstation):** download World Atlas + Scotland roads (once); run `generate_grid_v2.py` → new `grid.json`; `curl -X PUT` it to `s3://stars/grid.json`; `homelab scheduler jobs run-now stars.load_grid`; verify `stars.sites` repopulated with the new dark grid (spot-check: Cairngorms/Assynt now have points, cities don't).
3. Run `backfill_climatology.py` against the NEW grid → `climatology.json`; `curl -X PUT` to `s3://stars/climatology.json`; `homelab scheduler jobs run-now stars.load_climatology`; verify `stars.site_month_climatology` repopulated with clear-dark counts.
4. Eyeball `jomcgi.dev/app/stars` Historical: dark cells now over the Highlands, the monthly graph renders, winter > summer clear-dark hours.

Commit: `chore(monolith): bump chart for stars v2`.

---

## Risks / notes

- **Sea masquerading as darkest** in World Atlas (value ~0 over ocean) — already mitigated by the land mesh, but assert all kept points are on land.
- **No local test loop** — all `*_test.py` run only on CI; structure tests to be SQLite-`create_all` friendly (see `feedback_sqlite_test_fixtures_no_migrations`: mirror any new CHECK constraints in `__table_args__`).
- **gazelle:exclude stars** — new test targets (e.g. `stars_generate_grid_v2_test`) must be hand-added to `projects/monolith/BUILD`.
- **Data reset is intentional** — v1 history (287 live rows, 3672 climatology) is discarded; the new grid has different `site_id`s so old rows are meaningless.
- **Big offline downloads** (~650 MB + ~590 MB) are one-shot on the workstation; never in CI or the image.
- **Atlas migration checksums** — the format hook updates them; commit the regenerated checksums.
