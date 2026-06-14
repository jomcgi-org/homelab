# Ships Heatmap All-Time Vessel-Days Accumulator Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Replace the ships heatmap's sliding 7-day view with an all-time traffic-density map by banking each daily partition's per-cell vessel counts into a monotonic accumulator at partition-drop time and serving `historical + live` combined.

**Architecture:** A new `ships.heat_cells_historical` table holds a per-cell running total of "vessel-days" (sum of daily distinct movers). When `retention.py` drops a day's partition, it first banks that day's per-cell distinct-mover counts into the accumulator (`ON CONFLICT ... count = count + EXCLUDED.count`) in the **same transaction** as the `DROP`. The live `ships.heat_cells` 7-day rollup is unchanged. The `/api/ships/heat` endpoint merges both tables (summing per cell) and, because vessel-days grow unbounded, returns data-derived quantile color breakpoints so the frontend ramp self-calibrates instead of saturating.

**Key invariants (do not break):**

- A day's data lives in exactly one of {live `positions` window, banked `historical`}. The drop boundary is `today-8` (see `partitions_to_drop`), which is fully outside the live `now()-7d` window, so `all_time = live + historical` never double-counts.
- Banking reads `FROM ships.positions WHERE recorded_at >= day AND < day+1` (parent table, partition-pruned), **never** the child partition table by name. After the drop, a retry reads zero rows and adds zero: bank-and-drop is naturally idempotent even though `partitions_to_drop` re-scans a 30-day window each run. This is why the bank and drop MUST be in one transaction and the bank MUST select from the parent by date range.
- The metric is **vessel-days** (summable): each banked day contributes `count(distinct mmsi over movers)` for that day. A ferry crossing a cell daily adds +1/day. This is intentional (matches the stars `sum_q` bank-at-prune accumulator); do not try to make it a true all-time distinct count.

**Tech Stack:** Python 3 / SQLModel / FastAPI (monolith), Postgres (range-partitioned `ships.positions`), Atlas migrations, SvelteKit + MapLibre GL (frontend). SQLite + `create_all` for unit-test fixtures.

**Working directory:** `/tmp/claude-worktrees/ships-heat-alltime` (branch `feat/ships-heat-alltime`).

**Testing note (repo-specific):** There is NO local test loop. Do not run `pytest`/`bazel test` from the workstation. Author tests TDD-style in each task, commit, and defer ALL execution to the end-of-plan CI watch (Task 8). Implementer subagents self-review before committing.

---

### Task 1: Migration for `ships.heat_cells_historical`

**Files:**

- Create: `projects/monolith/chart/migrations/20260614140000_ships_heat_cells_historical.sql`
- Modify: `projects/monolith/chart/migrations/atlas.sum` (regenerated, do not hand-edit)

**Step 1: Write the migration**

Mirror the existing `20260611000000_ships_heat_cells.sql` style (header comment + `CREATE TABLE`). Content:

```sql
-- ships.heat_cells_historical: monotonic all-time traffic-density accumulator.
--
-- One row per ~500m grid cell holding the cumulative "vessel-days" of traffic:
-- the running SUM of each day's count(distinct moving mmsi) for that cell. Rows
-- are banked by ships.retention._run_partition_maintenance just before a daily
-- partition of ships.positions is dropped, so data that ages out of the live
-- 7-day window (ships.heat_cells) is preserved here instead of being lost.
--
-- Cell index matches ships.heat_cells: floor(lat / 0.005) x floor(lon / 0.0075).
-- The serving layer sums this table with the live ships.heat_cells to render the
-- all-time map. Banking is additive (ON CONFLICT DO UPDATE count = count +
-- EXCLUDED.count) and idempotent because the source partition no longer exists
-- on retry.

CREATE TABLE ships.heat_cells_historical (
    lat_bin     INTEGER NOT NULL,
    lon_bin     INTEGER NOT NULL,
    count       BIGINT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (lat_bin, lon_bin)
);
```

Note: `count` is `BIGINT` here (unbounded accumulation) where the live table uses `INTEGER`.

**Step 2: Regenerate the Atlas checksum**

Run: `atlas migrate hash --dir file://projects/monolith/chart/migrations`
Expected: `atlas.sum` updated to include the new file; no other changes.

**Step 3: Commit**

```bash
git add projects/monolith/chart/migrations/20260614140000_ships_heat_cells_historical.sql projects/monolith/chart/migrations/atlas.sum
git commit -m "feat(ships): add heat_cells_historical all-time accumulator table"
```

---

### Task 2: `HeatCellHistorical` model

**Files:**

- Modify: `projects/monolith/ships/models.py` (add class after `HeatCell`, ~line 84)
- Test: `projects/monolith/ships/models_test.py`

**Step 1: Write the failing test**

Add a test mirroring whatever the existing `HeatCell` test does (read `models_test.py` first for the fixture/style). Assert round-trip of a row:

```python
def test_heat_cell_historical_roundtrip(session):
    from ships.models import HeatCellHistorical

    session.add(HeatCellHistorical(lat_bin=10, lon_bin=-20, count=1234))
    session.commit()
    row = session.get(HeatCellHistorical, (10, -20))
    assert row is not None
    assert row.count == 1234
    assert isinstance(row.updated_at, datetime)  # naive under SQLite, see monolith CLAUDE.md
```

Match the existing test's session fixture and import conventions exactly.

**Step 2: Add the model**

```python
class HeatCellHistorical(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "heat_cells_historical"
    __table_args__ = {"schema": "ships", "extend_existing": True}

    # Monotonic all-time traffic accumulator: cumulative vessel-days (sum of each
    # dropped day's distinct-mover count) per ~500m cell. Banked at partition drop
    # by ships.retention; summed with the live HeatCell rollup by the serving layer.
    lat_bin: int = Field(primary_key=True)
    lon_bin: int = Field(primary_key=True)
    count: int
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
```

Confirm `datetime`/`timezone` are already imported in `models.py` (they are, used by other models).

**Step 3: Commit**

```bash
git add projects/monolith/ships/models.py projects/monolith/ships/models_test.py
git commit -m "feat(ships): add HeatCellHistorical model"
```

---

### Task 3: `bank_day_sql` pure builder in `heat.py`

**Files:**

- Modify: `projects/monolith/ships/heat.py` (add builder near `rollup_insert_sql`, ~line 76)
- Test: `projects/monolith/ships/heat_test.py`

**Step 1: Write the failing test**

Read `heat_test.py` first for the existing `rollup_insert_sql` test style (these assert on the generated SQL string, since the aggregation is Postgres-only). Add:

```python
def test_bank_day_sql_targets_historical_and_filters_one_day():
    from datetime import date
    from ships.heat import bank_day_sql

    sql = bank_day_sql(date(2026, 6, 1), 0.005, 0.0075, 1.0)

    # Writes into the accumulator, additively.
    assert "INSERT INTO ships.heat_cells_historical" in sql
    assert "ON CONFLICT (lat_bin, lon_bin)" in sql
    assert "count = ships.heat_cells_historical.count + EXCLUDED.count" in sql
    # Reads the day's slice from the PARENT table by range (idempotent after drop),
    # never the child partition table by name.
    assert "FROM ships.positions" in sql
    assert "positions_20260601" not in sql
    assert "recorded_at >= '2026-06-01'" in sql
    assert "recorded_at < '2026-06-02'" in sql
    # Same movers + distinct-mmsi semantics as the live rollup.
    assert "max(speed) >= 1.0" in sql
    assert "count(distinct" in sql
```

**Step 2: Implement the builder**

Bound the day with `partition_bounds`-style ISO strings derived from the `date` (reuse `from datetime import timedelta` already imported). Keep the "all interpolated values are non-user-input" nosemgrep note like the other builders.

```python
from datetime import date as _date  # if `date` not already imported at top


def bank_day_sql(
    day: "date", lat_step: float, lon_step: float, min_speed: float
) -> str:
    """Build the INSERT...SELECT that banks ONE day's per-cell distinct-mover
    counts into ships.heat_cells_historical, additively.

    Reads the day's rows from the PARENT partitioned table by recorded_at range
    (Postgres prunes to the one partition). After that partition is dropped this
    SELECT returns zero rows, so re-running adds nothing: the bank is idempotent
    and is therefore safe to run in the same transaction as the DROP and to
    re-attempt on later maintenance passes.

    `day`, steps and min_speed are derived from a date / module env constants,
    never user input, so the f-string cannot carry SQL injection. (Flagged to
    preempt the semgrep raw-SQL rule, mirrors rollup_insert_sql.)
    """
    lo = day.isoformat()
    hi = (day + timedelta(days=1)).isoformat()
    return (
        "INSERT INTO ships.heat_cells_historical (lat_bin, lon_bin, count) "
        "WITH movers AS ("
        "  SELECT mmsi FROM ships.positions"
        f"  WHERE recorded_at >= '{lo}' AND recorded_at < '{hi}'"
        f"  GROUP BY mmsi HAVING max(speed) >= {min_speed}"
        ") "
        f"SELECT floor(p.lat / {lat_step})::int, floor(p.lon / {lon_step})::int, "
        "       count(distinct p.mmsi) "
        "FROM ships.positions p JOIN movers USING (mmsi) "
        f"WHERE p.recorded_at >= '{lo}' AND p.recorded_at < '{hi}' "
        "  AND p.lat BETWEEN -90 AND 90 AND p.lon BETWEEN -180 AND 180 "
        "  AND NOT (p.lat = 0 AND p.lon = 0) "
        "GROUP BY 1, 2 "
        "ON CONFLICT (lat_bin, lon_bin) DO UPDATE "
        "SET count = ships.heat_cells_historical.count + EXCLUDED.count, "
        "    updated_at = now()"
    )
```

Ensure `date` is importable for the type hint (the module already imports `from datetime import datetime`; extend to `from datetime import date, datetime` if needed, matching `retention.py`).

**Step 3: Commit**

```bash
git add projects/monolith/ships/heat.py projects/monolith/ships/heat_test.py
git commit -m "feat(ships): add bank_day_sql builder for heat accumulator"
```

---

### Task 4: Wire banking into partition maintenance

**Files:**

- Modify: `projects/monolith/ships/retention.py:113-130` (`_run_partition_maintenance`)
- Test: `projects/monolith/ships/retention_test.py` (string/ordering assertions only; live DDL is Postgres-only and not unit-tested, per the module docstring)

**Step 1: Implement the wiring**

In `_run_partition_maintenance`, for each `day` in `partitions_to_drop(...)`, execute the bank INSERT **then** the drop, in the existing single transaction (one `session.commit()` at the end). Import the builder and the heat cell-step / min-speed constants from `ships.heat` so the cell index matches the live rollup exactly.

```python
from ships.heat import LAT_STEP, LON_STEP, MIN_SPEED_KN, bank_day_sql
...
            for day in partitions_to_drop(today, RETENTION_DAYS, DROP_SCAN_DAYS):
                # Bank the day's per-cell counts into the all-time accumulator
                # BEFORE dropping the partition, in this same transaction. The
                # bank reads the day's slice from the parent table by range, so a
                # retry after the drop reads zero rows (idempotent).
                session.execute(
                    text(bank_day_sql(day, LAT_STEP, LON_STEP, MIN_SPEED_KN))
                )
                session.execute(text(drop_partition_sql(day)))
            session.commit()
```

Update the success log line to mention banking (e.g. `"ships partition maintenance ok (retention=%dd, banked+dropped %d days)"` with `len(partitions_to_drop(...))`). Keep the `except: rollback` so a failure banks nothing and drops nothing (atomicity).

**Step 2: Add/adjust the unit test**

Read `retention_test.py` first. The handler itself isn't SQLite-testable, but if there are pure-helper tests you can add one asserting `bank_day_sql` precedes `drop_partition_sql` for a given day at the string level, or simply that importing the module wires the constants. Do not attempt to execute partition DDL under SQLite. If there is no natural seam, add a focused test that `bank_day_sql(day, ...)` and `drop_partition_sql(day)` reference the same partition day, documenting the bank-before-drop ordering contract.

**Step 3: Commit**

```bash
git add projects/monolith/ships/retention.py projects/monolith/ships/retention_test.py
git commit -m "feat(ships): bank heat cells into accumulator before dropping partitions"
```

---

### Task 5: Serving — merge live + historical and emit quantile stops

**Files:**

- Modify: `projects/monolith/ships/router.py:200-221` (`get_heat`), plus imports (~line 27) and two new pure helpers
- Test: `projects/monolith/ships/router_test.py`

**Step 1: Write failing tests for the pure helpers**

```python
def test_merge_cells_sums_overlapping_bins():
    from ships.router import _merge_cells

    live = [(1, 2, 5), (3, 4, 10)]
    hist = [(1, 2, 100), (5, 6, 7)]
    merged = dict(((la, lo), c) for la, lo, c in _merge_cells(live, hist))
    assert merged[(1, 2)] == 105   # summed
    assert merged[(3, 4)] == 10    # live-only
    assert merged[(5, 6)] == 7     # historical-only


def test_quantile_stops_ascending_unique_and_capped():
    from ships.router import _quantile_stops

    stops = _quantile_stops([1, 1, 1, 2, 5, 8, 13, 21, 34, 55, 89, 144])
    assert stops == sorted(set(stops))     # strictly ascending, deduped
    assert len(stops) <= 6
    assert stops[0] >= 1


def test_quantile_stops_empty():
    from ships.router import _quantile_stops
    assert _quantile_stops([]) == []
```

**Step 2: Implement the helpers**

```python
def _merge_cells(live, historical):
    """Sum per-cell counts across the live 7-day rollup and the all-time
    accumulator. Each input is an iterable of (lat_bin, lon_bin, count).
    Returns a list of (lat_bin, lon_bin, count). No double-counting: a day is
    either live or banked, never both (see retention.py invariant)."""
    totals: dict[tuple[int, int], int] = {}
    for lat_bin, lon_bin, count in (*live, *historical):
        totals[(lat_bin, lon_bin)] = totals.get((lat_bin, lon_bin), 0) + count
    return [(la, lo, c) for (la, lo), c in totals.items()]


def _quantile_stops(counts, n=6):
    """Up to ``n`` ascending, unique color breakpoints derived from the cell
    count distribution, so the stepped ramp self-calibrates as the all-time
    totals grow (a fixed 1/3/6/.. ramp would saturate to all-red). The first
    stop is always 1 (the lowest occupied bucket). Empty input -> []."""
    values = sorted(c for c in counts if c > 0)
    if not values:
        return []
    # Evenly spaced quantiles across the distribution.
    raw = [1]
    for i in range(1, n):
        q = i / n
        idx = min(len(values) - 1, int(q * len(values)))
        raw.append(values[idx])
    # Strictly ascending + unique (collapses to fewer stops on flat data).
    out: list[int] = []
    for v in raw:
        if not out or v > out[-1]:
            out.append(int(v))
    return out
```

**Step 3: Update the endpoint**

Import `HeatCellHistorical` alongside `HeatCell`. Read both tables, merge, compute stops, return them. Keep the existing response keys (`step_lat`, `step_lon`, `count`, `cells`) and ADD `stops`.

```python
from ships.models import HeatCell, HeatCellHistorical, LatestPosition, Position, Vessel
...
@router.get("/heat")
def get_heat(response: Response, session: Session = Depends(get_session)):
    """All-time traffic-density grid for the /app/ships heatmap.

    Sums the live 7-day rollup (HeatCell) with the all-time accumulator
    (HeatCellHistorical, banked at partition drop) per cell, so the map shows
    cumulative vessel-days rather than only the last 7 days. Returns data-derived
    quantile color stops so the stepped ramp self-calibrates. SSR-only, CDN-cached.
    """
    live = [(c.lat_bin, c.lon_bin, c.count) for c in session.exec(select(HeatCell)).all()]
    hist = [
        (c.lat_bin, c.lon_bin, c.count)
        for c in session.exec(select(HeatCellHistorical)).all()
    ]
    merged = _merge_cells(live, hist)
    cells = [[la, lo, c] for la, lo, c in merged]
    stops = _quantile_stops([c for _, _, c in merged])

    response.headers["Cache-Control"] = _HEAT_CACHE_CONTROL
    return {
        "step_lat": HEAT_LAT_STEP,
        "step_lon": HEAT_LON_STEP,
        "count": len(cells),
        "cells": cells,
        "stops": stops,
    }
```

If `router_test.py` has an existing `/heat` endpoint test asserting the response shape, extend it to seed a `HeatCellHistorical` row and assert the merged count and presence of `stops`.

**Step 4: Commit**

```bash
git add projects/monolith/ships/router.py projects/monolith/ships/router_test.py
git commit -m "feat(ships): serve all-time heat (live + accumulator) with quantile stops"
```

---

### Task 6: Frontend — data-driven ramp + legend from `stops`

**Files:**

- Modify: `projects/monolith/frontend/src/lib/public/components/ships/ShipsMap.svelte` (heat section, ~lines 90-200)
- Read first: the same file's `HEAT_STOPS`, `HEAT_PAINT`/`step` expression, legend markup, and `buildHeatGeoJSON`.

**Step 1: Make the ramp consume `data.stops`**

Keep the existing 6-color palette as `HEAT_COLORS` (extract the `color` values from the current `HEAT_STOPS`). When heat data loads, build the MapLibre `step` expression from `data.stops` (numeric ascending) zipped with `HEAT_COLORS`, taking only as many colors as there are stops. Fall back to the current fixed `HEAT_STOPS` breakpoints if `data.stops` is missing or empty (defensive: older cached payloads, empty grid).

Sketch:

```js
const HEAT_COLORS = [
  "#7b2ff7",
  "#d61f9c",
  "#ff0a78",
  "#ff6a00",
  "#ff2a1f",
  "#ff0019",
];
const FALLBACK_BREAKS = [1, 3, 6, 10, 15, 20];

function rampFor(stops) {
  const breaks = stops && stops.length ? stops : FALLBACK_BREAKS;
  const expr = ["step", ["get", "count"], HEAT_COLORS[0]];
  for (let i = 1; i < breaks.length && i < HEAT_COLORS.length; i++) {
    expr.push(breaks[i], HEAT_COLORS[i]);
  }
  return { expr, breaks };
}
```

Apply `expr` via `map.setPaintProperty(HEAT_LAYER, "fill-color", expr)` after the source data is set (the layer can be created with a placeholder paint, then updated once `stops` arrive).

**Step 2: Make the legend dynamic**

Render the legend swatches from `breaks` + `HEAT_COLORS`, with labels derived from consecutive breaks (`"1-2"`, ..., last is `"N+"`). Replace the hardcoded `label` strings. If `breaks === FALLBACK_BREAKS` (no data), the legend still renders sensibly.

**Step 3: Update the Heat-mode title/copy if it says "7-day"**

Grep the file (and `+page.svelte`) for any "7-day"/"last 7 days" heat copy and change it to reflect all-time traffic. Adjust the heat docstring comment at line ~90.

**Step 4: Commit**

```bash
git add projects/monolith/frontend/src/lib/public/components/ships/ShipsMap.svelte
git commit -m "feat(ships): data-driven heat ramp + legend for all-time view"
```

---

### Task 7: Chart version bump

**Files:**

- Modify: `projects/monolith/chart/Chart.yaml` (`version`)
- Modify: `projects/monolith/deploy/application.yaml` (`targetRevision`)

**Step 1: Bump both in sync**

Read the current `version` in `chart/Chart.yaml`, bump the patch (or minor if you judge this a feature release), and set the SAME value in `deploy/application.yaml` `targetRevision`. Per repo CLAUDE.md the `chart-version-bot` may also do this, but set both manually so the PR is self-consistent.

**Step 2: Commit**

```bash
git add projects/monolith/chart/Chart.yaml projects/monolith/deploy/application.yaml
git commit -m "chore(monolith): bump chart version for ships all-time heat"
```

---

### Task 8: Push, open PR, watch CI (end-of-plan verification)

**Step 1: Push and open the PR**

```bash
git push -u origin feat/ships-heat-alltime
gh pr create --fill --base main
```

**Step 2: Watch CI** (this is where ALL tests run; there is no local loop)

```bash
gh pr checks <number> --watch
```

On failure, fetch the actual log via `mcp__buildbuddy__get_invocation` (use the `commitSha` selector) → `get_target` → `get_log`, quote the verbatim assertion error before hypothesizing, fix, push. Watch for: Atlas migration lint/`atlas.sum` drift, semgrep raw-SQL rule on the new builders (the nosemgrep notes should cover it; if not, exclude in BUILD per repo convention), and the frontend build.

**Step 3: One comprehensive code review** (per repo CLAUDE.md: one review per merged PR, at the end, against the full diff).

**Step 4: Merge** with `gh pr merge --rebase` (rebase-only repo) after CI is green.

---

## Out of scope / follow-ups

- **No backfill** of already-dropped days: data that aged out before this ships has no banked counts. The accumulator starts empty and begins filling as partitions cross the `today-8` boundary (~1 day post-deploy the all-time view equals the live view; it diverges as days bank).
- **Paint tuning**: quantile stops are a first cut; the bucket count (`n=6`) and quantile spacing may need tuning against real accumulated data (mirrors the stars "heat-cell paint tuning" follow-up).
- **Live-only toggle**: deliberately dropped (decision: all-time only). Could be re-added later by exposing `?window=7d` on the endpoint.
