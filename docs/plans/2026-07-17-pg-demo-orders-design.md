# Design: Postgres demo panel v2 (orders ledger)

Date: 2026-07-17
Status: Approved
Scope: `projects/monolith/frontend/src/lib/private/components/demos/PostgresPanel.svelte`,
`projects/monolith/demos/firecracker_api.py` (+ tests), monolith chart bump.

## Problem

The Firecracker demos Postgres tab proves scale-to-zero Postgres (bank after ~1s
idle, wake on connect, data intact) but undersells it:

- The headline claim (instant Postgres with zero idle usage) reads as a gray
  status chip, not an exciting fact.
- One "Query (wakes the VM)" button conflates waking, writing, and reading, and
  no actual SQL is ever shown.
- The visit-note log renders as log lines; nothing makes the tabular,
  queryable nature of Postgres relatable.
- There is no way to clear accumulated rows (truncate).

## Decision

Replace the visit guestbook with an orders ledger: a domain everyone reads
instantly, where aggregation queries (SUM of order value) have obvious meaning.
Content is a means; the goal is making the tabular nature relatable.

### 1. Schema: new table `demo_orders` (DDL-if-missing, replaces `demo_visits` code path)

```sql
CREATE TABLE IF NOT EXISTS demo_orders (
  id bigserial PRIMARY KEY,
  item text NOT NULL,
  qty int NOT NULL,
  unit_price numeric(8,2) NOT NULL,
  written_at timestamptz NOT NULL DEFAULT now(),
  postmaster_start timestamptz NOT NULL
);
```

`postmaster_start` (pg_postmaster_start_time() at insert) keeps the lifecycle
proof: rows sharing it were served by one resumed process; older rows surviving
a new value show the volume outliving the VM. The old `demo_visits` table is
left in place but no longer read.

### 2. Two verbs, both wake the VM

- **Insert order**: the backend picks from a small fixed menu (coffee 3.50,
  keyboard 89.00, GPU 1999.00, rubber duck 1.20; qty randomised server-side)
  so the visitor never types. Runs `INSERT ... RETURNING id`, then the recent
  rows SELECT.
- **Run aggregate**: read-only roundtrip proving wake-without-write:

```sql
SELECT item, sum(qty) AS units, sum(qty * unit_price) AS revenue
FROM demo_orders GROUP BY item ORDER BY revenue DESC;
```

Backend: the existing `POST /postgres/query` gains `mode: "insert" | "aggregate"`
(the free-text note field is dropped). Both modes return the two bracketed wall
times (connect = the wake, sql roundtrip), the recent rows, totals, and the
verbatim SQL executed per statement with its measured ms.

A **statement strip** above the results shows the real SQL that just ran and
its timing. This is the core "real pg table" move: see the SQL, then the grid
it produced.

### 3. Truncate

New `POST /postgres/truncate` endpoint running `TRUNCATE demo_orders`.
Destructive-styled "clear the ledger" button with one inline confirm (private
tier only). Copy contrasts the two destructive verbs: truncate keeps the VM and
kills the data; reset (force cold boot) keeps the data and kills the VM.

### 4. Table rendered as a pg grid

- Monospace grid with type badges in headers (`id bigserial`, `item text`,
  `qty int`, `unit_price numeric`).
- psql-style `(15 rows)` footer.
- Epoch bands grouping rows by `postmaster_start`: "process 3, born 4:20 PM,
  current" / "process 2, survived a cold boot".
- Newest inserted row animates in.
- Aggregate results render as a compact totals card ("4,212.70 total revenue,
  37 orders") plus the per-item breakdown, distinct from the raw rows grid.

### 5. Excitement layer (frontend only, existing status payload)

- **Asleep hero strip**: when banked, the headline is the claim itself:
  "0 vCPU, 0 MiB RAM right now. 143.6 MiB of orders waiting on disk." The idle
  zero-cost fact is the hero, not a gray chip.
- **Live wake stopwatch**: while a connect is in flight, a counting-up ms timer
  with lifecycle narration from the sub-second poll (parking your TCP connect,
  relighting, spliced through). The final connect number lands with a flash.
- **Falling-asleep countdown**: while awake and idle, "falls asleep in ~1s"
  driven by `last_active_at`, so visitors watch it doze off live.

## Non-goals

- No embervm / control-plane changes; the workload config is untouched.
- No public-tier exposure; the panel stays private.
- No schema migrations tooling (DDL-if-missing inline, as today).

## Verification

Backend tests in `firecracker_api_test.py` (mode handling, truncate, in-band
errors). Frontend verified by render; end-to-end via CI on the pushed branch,
then live on the demos page after the chart bump deploys.
