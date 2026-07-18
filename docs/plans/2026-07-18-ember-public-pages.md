# Ember Public Pages Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.
> Repo overrides: NO local test execution (CI on push); ONE comprehensive review per PR; never use em-dashes anywhere.

**Goal:** Expose the scale-to-zero Postgres demo publicly at jomcgi.dev/ember/postgres (Turnstile-gated, rate-limited, health-alerted) and move the firecracker explainer to /ember/firecracker, per docs/plans/2026-07-18-ember-public-pages-design.md.

**Architecture:** Two PRs. PR 1 creates a public-safe `ember_public` python package (the demo-postgres core moves there; `demos` is forbidden in the public closure), registered via the FastMonolith public module registry, with Turnstile session gating, per-session insert bucket, global semaphore, status cache, savings grants + cached endpoint, and a demo_postgres /health component. PR 2 builds the public pages.

**Worktree:** /tmp/claude-worktrees/ember-public, branch feat/ember-public-pages.

**Key recon facts (verified 2026-07-18):**
- Public module registry: `app/modules_public.py` PUBLIC_MODULES tuple; a module contributes `register_public(app)` (see chat_public/module.py + chat_public/__init__.py). `framework/core.py build_app()` mounts them. The private app has the equivalent private registry (find `app/modules.py` / PRIVATE list and mirror).
- `main_public_imports_test.py` FORBIDDEN_MODULES includes "demos": the public package must NOT import demos. Private demos/ importing ember_public is fine.
- Public DB engines: default `app.db` = public_reader on the read replica; writes go through a separate engine per chat_public/db.py using PUBLIC_WRITER_DATABASE_URL (public_writer on primary). Turnstile verify helper with retries + fail-closed already exists: chat_public/turnstile.py `siteverify()`. TURNSTILE_SECRET_KEY / SITE_KEY already sync via the `cloudflare-turnstile` OnePasswordItem into monolith-public.
- Deep /api/health lives hardcoded in framework/core.py behind profile.deep_health (PUBLIC_PROFILE only); SvelteKit /health proxies it (frontend/src/routes/public/health/+server.js, 60s cache, 503 never cached).
- Semaphore/limit precedents: chat_public/limits.py (advisory-lock global slots + in-process fallback), hikes/forecast.py asyncio.Semaphore.
- Visual regression pages are enumerated in frontend/visual/targets.json + mock-server.mjs fixtures.
- /app/firecracker references to update: frontend/src/lib/public/apps.js, frontend/src/routes/public/HomepageRack.svelte (2 links), frontend/visual/targets.json.

---

## PR 1: backend (tier-neutral, verifiable on private tier alone)

### Task 1: ember_public package: move the demo-postgres core

**Files:** Create `projects/monolith/ember_public/` (`__init__.py`, `module.py`, `router.py`, `core.py`, `models.py` moved from demos/models.py, `router_test.py` etc.); Modify `projects/monolith/demos/firecracker_api.py` (slim to reset + goose/python/semgrep demos), `projects/monolith/app/modules_public.py`, the private module registry, `projects/monolith/demos/firecracker_api_test.py` (move pg tests), `projects/monolith/BUILD` if package registration needs it (check BOTH the _BACKEND_SRCS constant AND the py_library inline glob AND the public binary glob; new top-level packages have missed one before).

Move verbatim from demos/firecracker_api.py into ember_public/core.py: the DSN/turnstile-env helpers, session salt/tag, menu/DDL/statement constants, `_demo_pg_orders_roundtrip`, `_classify_wake`, `_shape_pg_status`, `_fetch_demo_pg_status` (EMBERVM_URL read from env directly, NOT imported from sandbox.client if that module is public-forbidden: check FORBIDDEN list; if sandbox is not importable publicly, read `os.environ["EMBERVM_URL"]` via a local helper), savings accrual (core/sync/async) and DemoPgSavings model. Public router (router.py) mounts at `/api/ember/postgres`: `GET /status`, `POST /query`, `POST /session`, `GET /savings`. `register_public(app)` includes it; register the same router on the PRIVATE app via the private registry so both tiers serve identical paths. Session endpoint: replace the hand-rolled `_verify_turnstile` with `chat_public.turnstile.siteverify` (it retries and fails closed; confirm chat_public import is legal in the public closure: it is in PUBLIC_MODULES).

demos/firecracker_api.py keeps ONLY `POST /postgres/reset` (private-only griefing-sensitive endpoint) plus the non-postgres demo endpoints, importing shared pieces from ember_public. The old private paths `/api/demos/firecracker/postgres/{status,query,session}` are REMOVED (the private panel migrates in Task 5).

Tests: move all demo-pg tests to ember_public/router_test.py updating paths to /api/ember/postgres; keep reset tests in firecracker_api_test.py. New test: the public app (main_public) serves /api/ember/postgres/status and does NOT serve any /api/demos route.

Commit: `feat(ember): public-safe ember_public package for the postgres demo`

### Task 2: gating: Turnstile enforcement, insert bucket, semaphore, status cache

**Files:** `projects/monolith/ember_public/core.py`, `router.py`, tests.

- Status cache: module-level `{at, payload}` guarding `_fetch_demo_pg_status` with a 0.5 s TTL (async lock; single flight). Status handler and health both read through it.
- Global semaphore: `asyncio.Semaphore(int(os.environ.get("EMBER_DEMO_MAX_CONCURRENT", "4")))` acquired non-blocking around the query roundtrip; when unavailable return in-band `{"error": "busy, one moment", "busy": true, ...}` (the frontend backoff already retries in-band errors).
- Insert bucket: in-process dict session_tag -> last_insert_monotonic; insert mode with a session younger than 5 s since its last insert returns in-band `{"error": "one order per five seconds", "rate_limited": true}`. No session on insert: allowed when TURNSTILE_SECRET_KEY is unset (private tier), rejected in-band ("solve the challenge first") when set (public). Aggregate stays session-optional. Dict is bounded (drop entries older than an hour on access).
- Session endpoint uses siteverify (Task 1) and its existing-cookie/mint logic unchanged.

Tests for each behavior (env-patched public vs private mode, semaphore exhaustion via patched roundtrip that blocks, bucket rejection then acceptance after patched clock).

Commit: `feat(ember): turnstile gating, insert rate limit, semaphore, and status cache`

### Task 3: savings on the public tier: grants, writer engine, cached endpoint

**Files:** New migration `projects/monolith/chart/migrations/20260718010000_demo_pg_savings_public_grants.sql`; `projects/monolith/ember_public/core.py`; grants test (mirror chat_public_grants_test.py); tests.

- Migration: `GRANT SELECT ON demo_pg_savings TO public_reader; GRANT SELECT, INSERT, UPDATE ON demo_pg_savings TO public_writer;` with a comment citing ADR security/005's narrow-grant precedent and why public status polls must accrue (generation-validated credit rule discards gaps it did not observe).
- Accrual engine selection: on the public profile use the writer engine (mirror chat_public/db.py: a small ember_public/db.py `get_savings_engine()` returning the PUBLIC_WRITER_DATABASE_URL engine when that env is set, else app.db.get_engine()). The accrual sync helper uses it; failure still degrades to omitting the field.
- `GET /api/ember/postgres/savings`: returns `{"total_saved_mib_s": float, "as_of": iso}` from a 30 s in-process cache over a SELECT via the default (reader) engine; missing table -> `{"total_saved_mib_s": null}`.

Commit: `feat(ember): public savings grants, writer-engine accrual, cached savings endpoint`

### Task 4: /health demo_postgres component

**Files:** `projects/monolith/framework/core.py` (minimal extension), `projects/monolith/ember_public/health.py`, tests.

Framework: give modules an optional `register_health` hook (name -> async check returning `{"ok": bool, "detail": str}`) collected by build_app and executed by the deep /api/health handler; overall status is 503 if any component not ok. Keep the diff minimal and mirror existing framework idioms.

ember_public health check (never connects to the demo DB):
1. Control-plane status (through the Task 2 cache) unreachable or unconfigured -> not ok.
2. `pair_valid` false while banked -> not ok.
3. Stuck transition: the status cache records `state_changed_at` whenever the observed state differs from the previous observation; transitional states (relighting, cold_booting, starting, banking) persisting > 90 s -> not ok (wakeTimeoutSeconds is 60 s plus margin).
4. Last real wake: the query path records `{at, ok, connect_ms}` module-level; a failed attempt or connect_ms > 60000 within the last 10 minutes, with no newer success, -> not ok.

Tests: each condition, plus /api/health 503 propagation on the public app.

Commit: `feat(ember): demo_postgres health component with stuck-boot and slow-wake detection`

### Task 5: private panel path migration + values wiring

**Files:** `projects/monolith/frontend/src/lib/private/components/demos/PostgresPanel.svelte` (API base -> `/api/ember/postgres`; reset stays `/api/demos/firecracker/postgres/reset`); `projects/monolith-public/deploy/values.yaml` (+ chart env templating if needed): `DEMO_POSTGRES_DSN` from the SAME 1Password item the private monolith uses (find it in projects/monolith/deploy/values.yaml and reference the same vault path via a new OnePasswordItem on the public chart), `EMBERVM_URL` (same value as private), `EMBER_DEMO_MAX_CONCURRENT: "4"`. TURNSTILE_* already present on the public tier. Private monolith values: no change (TURNSTILE_SECRET_KEY stays unset there).

Commit: `feat(ember): private panel on ember paths and public-tier demo env wiring`

### Task 6: PR 1 ship

fast-format (revert gazelle BUILD drift per the known issue), `bump-chart.sh projects/monolith` (bumps monolith-public too), push, PR, CI watch, comprehensive review at PR level, merge, rollout watch by image sha, live verify: private panel works end to end on the new paths; public backend (port-forward monolith-public service) serves /api/ember/postgres/status + savings and /api/health includes demo_postgres.

## PR 2: frontend (/ember pages)

### Task 7: /ember/postgres page: proxies, console, Turnstile widget

**Files:** Create `frontend/src/routes/public/ember/postgres/+page.svelte`, `+page.server.js` (passes TURNSTILE_SITE_KEY from env, initial cached status/savings fetch), same-origin proxy routes `frontend/src/routes/public/ember/postgres/api/{status,query,session,savings}/+server.js` each proxying `${API_BASE}/api/ember/postgres/<x>` (cookie pass-through for session/query: forward the demo_pg_session cookie both directions; follow health/+server.js timeout idiom); create `frontend/src/lib/public/ember/EmberConsole.svelte` as the public adaptation of the private PostgresPanel (no reset button, endpoints at the proxy paths, Turnstile widget gate before first insert: render the widget lazily when the visitor first focuses an insert action OR on mount if trivial; token posts to the session proxy; handle the no-JS/blocked-widget case with a plain message).
No `/ember` landing route is created (Joe owns it later); /ember returns 404 for now.

Commit: `feat(ember): public /ember/postgres live demo page`

### Task 8: the live ember stage

**Files:** `frontend/src/lib/public/ember/EmberStage.svelte` (+ small css), wired into the /ember/postgres page above the console.

Reuse the fcstory cell-grid technique (read FcScrollStory.svelte buildCells/setCells and fcstory.css): hot/cold cells with per-cell jittered thresholds, imperative rAF writes on plain element refs, palette via CSS custom properties in a colocated .css file (bare hex in .svelte is semgrep-blocked). Drive a target warmth [0,1] from the live status poll: banked 0, serving 1, relighting/cold_booting sweep 0->1 over ~the observed wake duration, banking sweep 1->0 over ~1.5 s; ease current warmth toward target each frame; the ragged edge comes from per-cell thresholds vs the sweep fraction. Overlay: state word + the all-time GB·h figure (from the savings payload) as the hero stat while cold, connect stopwatch while waking. prefers-reduced-motion: static two-state swap, no rAF loop.

Commit: `feat(ember): live-driven ember stage for the postgres demo`

### Task 9: explainer move + redirect + links + visual regression

**Files:** `git mv frontend/src/routes/public/app/firecracker` -> `frontend/src/routes/public/ember/firecracker`; new `frontend/src/routes/public/app/firecracker/+page.server.js` with `redirect(301, "/ember/firecracker")`; update apps.js href, HomepageRack.svelte (2 links), targets.json (firecracker path -> /ember/firecracker; add `{ "id": "ember-postgres", "path": "/ember/postgres" }`); add mock fixtures for the new page's proxied endpoints in frontend/visual/mock-server.mjs + fixtures/api/ (status: banked payload with total_saved_mib_s, savings, and a static rows fixture for the console's initial aggregate render; keep deterministic).

Commit: `feat(ember): move firecracker explainer to /ember and register visual targets`

### Task 10: PR 2 ship + live verification + alert

fast-format, bump-chart, push, PR, CI (visual regression will diff the moved page), review, merge, rollout watch. Live verification per the public-tier checklist: `curl -sS -o /dev/null -w '%{http_code}' https://jomcgi.dev/ember/postgres` (200), `/app/firecracker` returns 301 to /ember/firecracker, `https://jomcgi.dev/health` body includes demo_postgres ok, a real browser session (Turnstile) can insert, and an idle page leaves the VM banked (watch status stay banked while the page only polls). Check whether the existing /health httpcheck alert covers the new component (it does if it alerts on non-200); if a separate check is wanted, use the add-httpcheck-alert skill. Update memory files at the end.
