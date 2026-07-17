# Postgres Demo Orders-Ledger Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.
> Repo overrides apply: NO local test execution (tests run on BuildBuddy CI after push), and ONE comprehensive code review at end of PR, not per task.

**Goal:** Redesign the Firecracker demos Postgres tab into an orders ledger with insert/aggregate verbs, a truncate action, a pg-grid table rendering, and an excitement layer that makes scale-to-zero the headline.

**Architecture:** Backend (`projects/monolith/demos/firecracker_api.py`) swaps the visit-guestbook roundtrip for a `demo_orders` roundtrip with two modes (insert, aggregate) and adds a truncate endpoint; every response carries the verbatim SQL statements executed with per-statement timings. Frontend (`PostgresPanel.svelte`) is rewritten around three cards: lifecycle hero (asleep = "0 vCPU · 0 MiB RAM"), timing/statement strip, and the orders grid with type-badged headers and postmaster epoch bands.

**Tech Stack:** FastAPI + psycopg3 (sync, via `asyncio.to_thread`), Svelte 5 runes, existing CSS custom properties. Design doc: `docs/plans/2026-07-17-pg-demo-orders-design.md`.

---

## Repo ground rules for every task

- Worktree: `/tmp/claude-worktrees/pg-demo-orders`, branch `feat/pg-demo-orders`.
- Do NOT run pytest/bazel locally. Write tests; CI runs them after push (Task 5).
- Conventional Commits; the pre-commit format hook may modify files (docs manifests) — re-stage and re-commit if it does.
- Never use em-dashes in any text you write (code comments, copy, commits).
- Self-review your diff before each commit; the comprehensive review happens once at end of PR.

---

### Task 1: Backend orders roundtrip with insert/aggregate modes

**Files:**
- Modify: `projects/monolith/demos/firecracker_api.py` (the demo-postgres section, currently lines ~560-787)
- Modify: `projects/monolith/demos/firecracker_api_test.py` (postgres query tests, lines ~282-354)

**Step 1: Replace `_demo_pg_roundtrip` and the request model**

Delete `_demo_pg_roundtrip` and `PostgresQueryRequest` (note field). Add:

```python
# The fixed menu keeps the demo zero-typing: an insert picks a random line item
# server-side. Prices are illustrative; the aggregate query is the point.
_DEMO_PG_MENU = [
    ("flat white", 3.50),
    ("mechanical keyboard", 89.00),
    ("rubber duck", 1.20),
    ("gpu", 1999.00),
    ("ergonomic chair", 349.00),
    ("sticker pack", 4.75),
]

_DEMO_PG_DDL = (
    "CREATE TABLE IF NOT EXISTS demo_orders ("
    "  id bigserial PRIMARY KEY,"
    "  item text NOT NULL,"
    "  qty int NOT NULL,"
    "  unit_price numeric(8,2) NOT NULL,"
    "  written_at timestamptz NOT NULL DEFAULT now(),"
    "  postmaster_start timestamptz NOT NULL)"
)

_DEMO_PG_INSERT = (
    "INSERT INTO demo_orders (item, qty, unit_price, postmaster_start) "
    "VALUES (%s, %s, %s, pg_postmaster_start_time()) RETURNING id"
)

_DEMO_PG_RECENT = (
    "SELECT id, item, qty, unit_price, written_at, postmaster_start "
    "FROM demo_orders ORDER BY id DESC LIMIT %s"
)

_DEMO_PG_AGGREGATE = (
    "SELECT item, sum(qty) AS units, sum(qty * unit_price) AS revenue "
    "FROM demo_orders GROUP BY item ORDER BY revenue DESC"
)

_DEMO_PG_TOTALS = (
    "SELECT count(*), coalesce(sum(qty * unit_price), 0), "
    "pg_postmaster_start_time() FROM demo_orders"
)


def _demo_pg_orders_roundtrip(dsn: str, mode: str) -> dict:
    """Connect, run the mode's statements, and time each one. Sync; to_thread.

    connect_ms is the wake (the activator parks the TCP connect while the VM
    relights or cold-boots); each executed statement is returned verbatim with
    its own wall time so the UI can show the SQL that just ran. The connection
    is short-lived by design: an open connection pins the VM awake.

    insert    - DDL-if-missing, append a random menu line item, then read the
                recent rows, the aggregate breakdown, and the totals.
    aggregate - read-only: the same reads without writing anything, proving a
                wake needs no write.
    """
    started = perf_counter()
    conn = psycopg.connect(dsn, connect_timeout=_DEMO_PG_CONNECT_TIMEOUT_S)
    connect_ms = (perf_counter() - started) * 1000

    statements: list[dict] = []

    def run(cur, sql: str, params=None):
        stmt_started = perf_counter()
        cur.execute(sql, params)
        statements.append(
            {"sql": sql, "ms": (perf_counter() - stmt_started) * 1000}
        )

    inserted = None
    query_started = perf_counter()
    # psycopg3: the connection context commits on clean exit AND closes.
    with conn, conn.cursor() as cur:
        run(cur, _DEMO_PG_DDL)
        if mode == "insert":
            item, unit_price = random.choice(_DEMO_PG_MENU)
            qty = random.randint(1, 5)
            run(cur, _DEMO_PG_INSERT, (item, qty, unit_price))
            inserted = {
                "id": cur.fetchone()[0],
                "item": item,
                "qty": qty,
                "unit_price": unit_price,
            }
        run(cur, _DEMO_PG_RECENT, (_DEMO_PG_HISTORY_ROWS,))
        rows = [
            {
                "id": r[0],
                "item": r[1],
                "qty": r[2],
                "unit_price": float(r[3]),
                "written_at": r[4].isoformat(),
                "postmaster_start": r[5].isoformat(),
            }
            for r in cur.fetchall()
        ]
        run(cur, _DEMO_PG_AGGREGATE)
        breakdown = [
            {"item": r[0], "units": int(r[1]), "revenue": float(r[2])}
            for r in cur.fetchall()
        ]
        run(cur, _DEMO_PG_TOTALS)
        total_orders, total_revenue, postmaster_start = cur.fetchone()
    query_ms = (perf_counter() - query_started) * 1000

    return {
        "connect_ms": connect_ms,
        "query_ms": query_ms,
        "mode": mode,
        "statements": statements,
        "inserted": inserted,
        "rows": rows,
        "breakdown": breakdown,
        "total_orders": total_orders,
        "total_revenue": float(total_revenue),
        "postmaster_start": postmaster_start.isoformat(),
    }
```

Add `import random` to the module imports if absent. The DDL statement's timing stays in the statements list; the frontend filters it out of the strip (it is real, but the story statements are INSERT/SELECT).

**Step 2: Rework the endpoint**

```python
class PostgresQueryRequest(BaseModel):
    mode: Literal["insert", "aggregate"] = "insert"
```

(Import `Literal` from `typing` if not already imported.) In `postgres_query`, replace the note handling with:

```python
    result = await asyncio.to_thread(_demo_pg_orders_roundtrip, dsn, body.mode)
```

Keep everything else identical: pre-query status fetch, `_classify_wake`, in-band error shape (add `"mode": body.mode` to the error payload), and the success merge.

**Step 3: Update the tests**

In `firecracker_api_test.py`, rewrite `test_postgres_query_returns_timings_and_classification` to POST `{"mode": "insert"}`, fake `_demo_pg_orders_roundtrip(dsn, mode)` asserting `mode == "insert"`, and return the new shape (statements list, inserted, rows with item/qty/unit_price, breakdown, total_orders, total_revenue). Assert the response echoes `mode`, `statements`, `breakdown`, and `total_revenue`. Add `test_postgres_query_aggregate_mode` asserting `mode == "aggregate"` reaches the roundtrip and `inserted` is None in the response. Update `test_postgres_query_connect_failure_is_in_band` to patch `_demo_pg_orders_roundtrip`. Add a default-mode test: POST `{}` yields mode `insert`.

**Step 4: Self-review the diff, commit**

```bash
cd /tmp/claude-worktrees/pg-demo-orders
git add projects/monolith/demos/firecracker_api.py projects/monolith/demos/firecracker_api_test.py
git commit -m "feat(demos): orders-ledger roundtrip with insert and aggregate modes"
```

---

### Task 2: Truncate endpoint

**Files:**
- Modify: `projects/monolith/demos/firecracker_api.py` (after `postgres_reset`)
- Modify: `projects/monolith/demos/firecracker_api_test.py`

**Step 1: Implement**

```python
def _demo_pg_truncate(dsn: str) -> dict:
    """TRUNCATE demo_orders. Sync; via to_thread. Wakes the VM like any connect."""
    started = perf_counter()
    conn = psycopg.connect(dsn, connect_timeout=_DEMO_PG_CONNECT_TIMEOUT_S)
    connect_ms = (perf_counter() - started) * 1000
    with conn, conn.cursor() as cur:
        cur.execute(_DEMO_PG_DDL)
        cur.execute("TRUNCATE demo_orders")
    return {"truncated": True, "connect_ms": connect_ms}


@router.post("/postgres/truncate")
async def postgres_truncate() -> dict:
    """Clear the ledger. The data dies, the VM lives: the mirror image of reset
    (which destroys the VM and keeps the data). Private tier only, like the
    rest of this router. Errors come back in-band, mirroring the query shape.
    """
    dsn = _demo_pg_dsn()
    if not dsn:
        raise HTTPException(
            status_code=503, detail="DEMO_POSTGRES_DSN is not configured"
        )
    try:
        return await asyncio.to_thread(_demo_pg_truncate, dsn)
    except Exception as exc:  # noqa: BLE001 - surface connect failures in-band
        logger.warning("demo-postgres truncate failed: %s", exc)
        return {"truncated": False, "error": str(exc)}
```

**Step 2: Tests**

Add `test_postgres_truncate_unconfigured_is_503` (delenv DSN, expect 503) and `test_postgres_truncate_ok` (patch `_demo_pg_truncate` to return `{"truncated": True, "connect_ms": 5.0}`, assert echo) and `test_postgres_truncate_error_in_band` (patched helper raises OSError, expect 200 with `truncated: False` and the message in `error`).

**Step 3: Self-review, commit**

```bash
git add projects/monolith/demos/firecracker_api.py projects/monolith/demos/firecracker_api_test.py
git commit -m "feat(demos): truncate endpoint for the demo orders ledger"
```

---

### Task 3: Frontend rewrite: verbs, statement strip, pg grid

**Files:**
- Modify: `projects/monolith/frontend/src/lib/private/components/demos/PostgresPanel.svelte` (full rewrite of markup/logic; keep the existing lifecycle poll, state machine, tone CSS, and error handling patterns)

This task delivers the structural rewrite; Task 4 layers the animation/excitement work on top. Keep the existing conventions: `$state`/`$derived` runes, `var(--...)` theme tokens, in-band error handling, 700 ms status poll, auto-run on mount (mount fires an **aggregate** now, so opening the tab wakes the VM without writing).

**Controls row** (replaces note input + single Query button):
- Button "INSERT an order" -> `runQuery("insert")`. Subtext/tooltip: "appends a random line item from the menu".
- Button "Run aggregate" -> `runQuery("aggregate")`. Tooltip: "SELECT only: wakes the VM without writing".
- Button "Clear ledger (TRUNCATE)" with a two-click inline confirm (first click flips label to "really truncate?", second fires `POST /truncate`, any other interaction resets it). Destructive styling like the existing reset button.
- Keep "Force cold boot" (reset). Group the two destructive buttons; add a one-line caption under the controls: "truncate keeps the VM and kills the data; cold boot keeps the data and kills the VM".

**Statement strip** (new card, replaces the timing-note paragraph's role): after each run, list `lastRun.statements` minus the `CREATE TABLE` one, each as a monospace SQL line with a right-aligned `-> N ms`. SQL rendered verbatim from the backend (single source of truth for what ran).

**Orders grid** (replaces rows-table):
- Header cells show name + type badge stacked: `id` / `bigserial`, `item` / `text`, `qty` / `int`, `unit_price` / `numeric(8,2)`, `written_at` / `timestamptz`. Monospace numerals, right-aligned qty/price.
- Rows grouped into epoch bands by `postmaster_start`: a full-width band separator row per group, newest first, labelled `process born {clock(ps)}` plus `current` for the newest group or `survived a later boot` for older ones. Band tint via `color-mix` on `var(--accent)` alternating opacity.
- Footer line psql-style: `({total_orders} rows)`.
- `unit_price`/revenue formatted with 2 decimals and a currency prefix (use `£`).

**Aggregate card** (new, shown when `lastRun.breakdown` exists):
- Headline: `£{total_revenue} total revenue · {total_orders} orders` (big numerals, same style as timing values).
- Below: compact per-item bars: item name, units, revenue, with a proportional bar width by revenue share.

**Timing card**: keep as-is (connect + SQL roundtrip + classification + split bar), it already works; just make sure it reads `lastRun` from the new shape (`query_ms` unchanged).

Also update the `tiers` logic unchanged (it keys off `connect_ms`/`classification` which survive).

**Commit:**

```bash
git add projects/monolith/frontend/src/lib/private/components/demos/PostgresPanel.svelte
git commit -m "feat(demos): orders-ledger UI with insert/aggregate verbs and pg grid"
```

---

### Task 4: Excitement layer

**Files:**
- Modify: `PostgresPanel.svelte` (same file; additive)

**1. Asleep hero strip.** When `status.state === "banked"`, the lifecycle card grows a hero line above the facts, replacing the muted hint as the visual lead:

> **0 vCPU · 0 MiB RAM right now.** {volume MiB} of orders on disk, waiting. The next connection brings Postgres back in ~{best relight ms or "under 100 ms"}.

Style: larger type (18-20px), normal ink (not dim). The state chip stays. When not banked, the existing hint renders as today.

**2. Live wake stopwatch.** While `running` is true, the timing card area shows a counting timer: `performance.now()` delta rendered at ~60 fps via `requestAnimationFrame` into a `$state` value, formatted with `ms()`. Under it, narrate from the live poll: map `status.state` to a phrase (banked: "connection parked, waking the VM", relighting: "relighting from snapshot", cold_booting: "cold booting against the volume", serving: "spliced through, running SQL"). When the response lands, stop the raf loop and flash the final connect number (a one-shot CSS keyframe scale+color pulse, ~500 ms).

**3. Falling-asleep countdown.** When `status.state === "serving"` and not `running`: compute seconds since `status.last_active_at` each poll tick; show in the state hint: "idle, falls asleep about a second after the last connection closes" and when a wall-clock second has passed with no activity, "dozing off any moment". Do not fabricate a precise countdown (bank timing is the sweeper's call); phrase it as approach, not a timer.

**4. New-row animation.** After an insert, the row whose id matches `lastRun.inserted.id` gets a one-shot highlight animation (background fade from accent tint to transparent, 800 ms).

Respect `prefers-reduced-motion`: wrap the raf stopwatch flash and row animation keyframes in `@media (prefers-reduced-motion: no-preference)` (stopwatch may simply show the ticking number without pulse effects).

**Commit:**

```bash
git add projects/monolith/frontend/src/lib/private/components/demos/PostgresPanel.svelte
git commit -m "feat(demos): asleep hero, wake stopwatch, and doze narration for pg demo"
```

---

### Task 5: Format, chart bump, PR, CI, merge, verify live

**Step 1: Format + bump**

```bash
cd /tmp/claude-worktrees/pg-demo-orders
bazel/tools/format/fast-format.sh
bazel/tools/git/bump-chart.sh projects/monolith
git add -A
git commit -m "chore(monolith): bump chart for postgres demo orders ledger"
```

(The design-doc commit already touched the docs manifest; the code changes require the monolith bump regardless. If jomcgi.dev public assets changed too, they did not here: only the private panel, so only the monolith chart bumps.)

**Step 2: Push, PR, watch CI**

```bash
git push -u origin feat/pg-demo-orders
gh pr create --title "feat(demos): postgres demo orders ledger with insert/aggregate and truncate" --body "..."
gh pr checks <number> --watch
```

PR body summarizes the design doc; end with the standard generated-with footer. On CI failure: `mcp__buildbuddy__get_invocation` (commitSha selector) -> `get_target` -> `get_log`; quote the real error before hypothesizing; fix and push.

**Step 3: End-of-PR comprehensive review** (main session, Opus eyes): read the full diff once, checking especially: psycopg statement/param handling, in-band error shapes preserved, no em-dashes introduced, semgrep-sensitive patterns (no sync session misuse applies to scheduler jobs, not here, but `to_thread` usage must not capture a session), Svelte runes correctness, reduced-motion media queries.

**Step 4: Merge + verify live**

```bash
gh pr merge <number> --auto --rebase
```

Poll merge state (BEHIND -> `gh pr update-branch <number> --rebase`). After merge, watch the main-branch push run, then verify rollout: `kubectl get applications -n argocd` for monolith sync, then hit the demos page endpoints (`/api/demos/firecracker/postgres/status`) and confirm the new query shape live with one insert + one aggregate. Clean up the worktree.
