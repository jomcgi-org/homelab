# CLAUDE.md - monolith

Scoped guidance for `projects/monolith`. The repo-root `CLAUDE.md` still applies.
See `README.md` in this directory for the domain map and public/private tier boundary.

## Scheduled job handlers must not block the event loop

Handlers registered with `scheduler.api.register_job` have the signature
`async def handler(session: Session) -> datetime | None` and are **awaited on the
scheduler's event loop**. The monolith uses synchronous SQLModel sessions, so
calling a sync Session method (`session.add` / `exec` / `execute` / `commit` /
`get`) directly inside an `async def` blocks every coroutine on the loop,
including `/healthz`, for the duration of the query. Semgrep
`no-sync-session-in-async-def` fails CI on this; the rule is enforcing a real
production concern, not a style nit.

The established pattern (see `hikes/jobs.py`, `ships/retention.py`):

1. Do all network I/O in the async handler with `await` first.
2. Delegate **all** Session I/O to a worker thread:
   `await asyncio.to_thread(_sync_helper, data)`.
3. The sync helper opens its **own** fresh session and commits:
   ```python
   def _sync_helper(data) -> int:
       from app.db import get_engine
       with Session(get_engine()) as session:
           ...  # sync DB work
           session.commit()
   ```
   Pass plain data into `to_thread`, **never** the scheduler's `session` argument
   (semgrep `no-session-in-to-thread` blocks that, and a session is not safe to
   use across threads).
4. Keep the DB logic in a sync core that takes an explicit `session` parameter
   so the SQLite `create_all` test fixtures can drive it directly; the async
   wrapper (network + `to_thread`) stays thin and is not unit tested.
5. Do not `session.add` in a loop (semgrep `session-add-in-loop`): build the
   rows and `session.add_all(...)` once, or mutate `session.get`-tracked rows
   and let them flush on `commit`.
6. Pass `heavy=True` to `register_job` for memory-intensive jobs (e.g. graph
   layout). The dispatcher serializes heavy jobs so two of them never co-run
   and OOMKill the shared pod; light jobs stay fully parallel.

## Test fixtures use SQLite; datetimes come back naive

Model/endpoint tests use SQLite + `SQLModel.metadata.create_all` (not
migrations). SQLite has no tz-aware type, so a `TIMESTAMPTZ` column round-trips
as a **naive** datetime in tests, while production (Postgres) is tz-aware.

- Assert `isinstance(value, datetime)`, not `value.tzinfo is not None`.
- When comparing loaded datetimes for equality (sets, `==`), coerce first:
  `dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)`.
- Serialize datetimes through an `_as_utc`/`_iso` helper (see `ships/router.py`)
  so JSON output and ETags are offset-consistent across SQLite tests and
  Postgres prod.

## Public-page visual regression (`frontend/visual/`)

Every PR runs the `Visual regression` BuildBuddy action: it screenshots all
public SvelteKit pages against committed mock data, diffs the PR render against
the `origin/main` render, and posts inline before/after/diff images to the PR
for only the pages that changed. There are no committed baselines: `main` is the
baseline, rendered on demand in the same CI run. The tool is a standalone Node
package under `projects/monolith/frontend/visual/` (its own `package.json`),
deliberately NOT part of the hermetic app build and NOT a `bazel test`.

How it renders deterministically:

- **Mock seam is `API_BASE`.** Every public `+page.server.js` `load()` calls
  `fetch(\`${API_BASE}/api/...\`)`. The action boots the real adapter-node app
  (`:build_public`) with `API_BASE` pointed at `mock-server.mjs`, which serves
  committed JSON fixtures from `fixtures/api/`. The app's own `+server.js` proxy
  routes run inside the app and call the mock themselves, so the mock only needs
  the raw `/api/...` paths.
- **Maps are intercepted, not masked.** Playwright fulfils
  `tiles.openfreemap.org` with a committed flat `fixtures/basemap/blank-style.json`
  and `/img/**` with a placeholder PNG, so the basemap and trip thumbnails render
  a stable backdrop while the real overlay data (markers, routes, heat) diffs.
- **Determinism** comes from a frozen browser clock, software-GL chromium, and
  rendering BOTH the PR and `main` in the SAME pinned exec image in one CI run,
  so font hinting and WebGL match by construction. macOS vs Linux differs at the
  pixel level, so never compare a workstation render against a CI one.

How the branch-vs-main diff runs (no committed baselines, no sentinel): a path
gate (`git diff origin/main HEAD -- <frontend subtree, projects/websites/shared,
pnpm-lock, MODULE.bazel>`) skips both captures when nothing render-relevant
changed; the gate is a SUPERSET of `:capture`'s inputs, so being too broad only
costs a redundant cache-hit build, never a missed regression. Otherwise CI
renders the PR, then `git checkout origin/main` IN PLACE (one warm Bazel
output_base) and renders main, then diffs the two. The capture action is content
-addressed, so main's render is a remote-cache download whenever its frontend is
unchanged.

To add a public page: add it to `targets.json` and add any new `/api/...`
fixture to `mock-server.mjs` + `fixtures/api/`. There is nothing to "accept":
when the PR merges, main's render moves with it, so the next PR diffs against the
new truth automatically. Diff images are hosted as assets on the
`visual-snapshots` GitHub prerelease (the repo is public, so the URLs render
inline in the PR comment).
