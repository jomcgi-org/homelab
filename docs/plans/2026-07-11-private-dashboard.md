# Private Dashboard Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to
> implement this plan task-by-task in the current session. Per CLAUDE.md, defer ALL
> test execution to end-of-plan CI on the pushed branch; implementers write tests
> but do not run them locally. One comprehensive code review at end of PR, not per task.

**Goal:** Replace the private.jomcgi.dev landing page with a personal dashboard
(live homelab status + launcher + embedded cluster chat) and drop the shared nav
bar on the private tier.

**Architecture:** One new aggregation endpoint `GET /api/home/dashboard` (private
tier only) fans out server-side to existing internals with per-section error
isolation. One new PydanticAI agent at `POST /api/chat/cluster` wraps the existing
read-only k8s tools plus firing alerts, streaming SSE like the explorer agent.
The dashboard page is a rewrite of `routes/private/+page.svelte` keeping the
existing quick-capture and knowledge-search overlays; nav suppression happens in
the root layout.

**Tech Stack:** FastAPI + PydanticAI (Qwen via llama.cpp) backend, SvelteKit 5
(runes) frontend, no new dependencies.

**Design doc:** docs/plans/2026-07-11-private-dashboard-design.md

## Verified ground truth (do not re-derive)

- `GITHUB_TOKEN` is present in the MAIN monolith container env
  (`chart/templates/deployment.yaml:155`, secret `monolith-chat-secrets`).
- RBAC (`chart/templates/rbac.yaml`) already grants get/list on pods,
  deployments, statefulsets, daemonsets, events, nodes and argoproj.io
  applications. No RBAC changes needed.
- `/api/chat` is NOT on the private HTTPRoute; the existing chat is proxied by
  a SvelteKit `+server.js`. Follow that pattern; do not touch chart templates.
- `GET /api/home` (old todo) was deleted in the 2026-04-17 refactor and 404s.
  The live task endpoints are `GET /api/knowledge/tasks/daily`,
  `GET /api/knowledge/tasks/weekly`, `PATCH /api/knowledge/tasks/{note_id}`
  (`knowledge/tasks_router.py`).
- Health rollup internals: `cluster/mcp.py` `k8s_health_summary` builds from
  `KubernetesClient.list_resources(kind)` + `cluster.summarize.build_health`.
- Firing alerts: `agent/checks.py::check_firing_alerts()` (env `SIGNOZ_URL`,
  optional `SIGNOZ_API_KEY`), returns `list[dict]`.
- Scheduler jobs: `scheduler/service.py::list_jobs(session)` returns
  `list[SchedulerJobView]`.
- Today's events: `home/api.py::get_today_events(session)`.
- Knowledge queue counts: see `knowledge/router.py` review-queue and gaps
  review-queue endpoints for the store calls to reuse.
- Module boundaries (ADR platform/008, enforced by `import_boundaries_test`):
  cross-domain imports go through the domain's `api.py` facade. The home
  dashboard module needs facades for anything it pulls from `cluster`, `agent`,
  `scheduler`, `knowledge`. Check each domain's `api.py` and extend it if the
  needed function is not yet exported.
- The public tier (`app/main_public.py`) mounts `home.register_public`; the new
  dashboard endpoint must be registered ONLY in the private `home.register`.
- New public callables trip `bdd_completeness_test`; add BDD entries in the
  domain's tests (see `home/tests/bdd_api_test.py` for the pattern).
- Frontend is Svelte 5 runes (`$state`, `$derived`, `$effect`, `$props`).
- Design tokens: use the CSS custom properties already used by the private page
  (`--font`, `--bg`, `--fg`, `--fg-secondary`, `--fg-tertiary`, `--border`,
  `--surface`, `--danger`). No new colors, match the existing brutalist style.
- CLAUDE.md bans em-dashes in ALL new copy, comments and docs.

---

### Task 1: Backend dashboard aggregator (`/api/home/dashboard`)

**Files:**
- Create: `projects/monolith/home/dashboard.py`
- Create: `projects/monolith/home/dashboard_router.py`
- Create: `projects/monolith/home/dashboard_test.py`
- Modify: `projects/monolith/home/__init__.py` (register the router, private only)
- Modify: facades as needed (`cluster/api.py`, `agent/api.py`, `scheduler/api.py`,
  `knowledge/api.py`) so imports respect module boundaries
- Modify: `projects/monolith/home/tests/bdd_api_test.py` (completeness entries)

**Step 1: Read the neighbours.** Read `home/__init__.py`, `cluster/api.py`,
`agent/api.py` (if present, else `agent/__init__.py`), `scheduler/api.py`,
`knowledge/api.py`, `home/observability/router.py`, and one recent test file in
`home/` to copy conventions. Confirm which facade functions exist vs need adding.

**Step 2: Write `home/dashboard.py`.** Async collectors, each fail-soft:

```python
"""Dashboard aggregation for the private landing page.

One endpoint fans out to the existing domain internals so the page loads with a
single fetch. Every section is independently try/excepted: a dead collector
returns {"error": "..."} for its section and never blanks the others.
"""

async def _collect_health() -> dict      # cluster health rollup (build_health over the 5 kinds, same as k8s_health_summary)
async def _collect_alerts() -> dict      # {"firing": [...]} via agent facade check_firing_alerts()
async def _collect_github() -> dict      # open PRs + checks + last 5 merged, authed with GITHUB_TOKEN, 60s in-process TTL cache
async def _collect_queues(session) -> dict   # review queue count, gap queue count, scheduler jobs (failing first), tasks daily/weekly
async def _collect_today(session) -> dict    # {"events": get_today_events(session)}

async def build_dashboard(session) -> dict:
    # asyncio.gather all collectors with return_exceptions=True,
    # map exceptions to {"error": str(exc)} per section, plus cached_at.
```

GitHub collector detail: `GET /repos/jomcgi/homelab/pulls?state=open` and
`GET /repos/jomcgi/homelab/pulls?state=closed&sort=updated&direction=desc&per_page=10`
(filter `merged_at != null`, keep 5), and for each open PR
`GET /repos/jomcgi/homelab/commits/{head_sha}/check-runs` reduced to one of
`passing|failing|pending`. Headers: `Authorization: Bearer $GITHUB_TOKEN`,
`Accept: application/vnd.github+json`. `httpx.AsyncClient(timeout=10)`.
Module-level `_cache: tuple[float, dict] | None` with 60s TTL like the stats
pattern. Shape each PR as `{number, title, author, draft, ci, updated_at, url}`.

Health collector: reuse `cluster.summarize.build_health` and
`KubernetesClient.list_resources` through the `cluster` facade (add facade
exports if missing, mirroring how `home/observability/stats.py` imports
`cluster.api.KubernetesClient`).

Queues collector: reuse the exact store calls the review-queue endpoints in
`knowledge/router.py` make (counts only, not full payloads), plus
`store.list_tasks_daily()` / `store.list_tasks_weekly()`, plus scheduler
`list_jobs` mapped to `{name, last_status, last_run_at, next_run_at}` with
failing/stuck jobs sorted first.

**Step 3: Write `home/dashboard_router.py`:**

```python
router = APIRouter(prefix="/api/home", tags=["dashboard"])

@router.get("/dashboard")
async def get_dashboard(session: Session = Depends(get_session)) -> dict:
    return await build_dashboard(session)
```

**Step 4: Register in `home/__init__.py`** inside `register()` ONLY (not
`register_public`).

**Step 5: Write `home/dashboard_test.py`.** pytest (remember the repo gotcha:
the file must contain a literal `import pytest`). Test per-section error
isolation (monkeypatch one collector to raise, assert others survive and the
broken one returns `{"error": ...}`), GitHub cache TTL behaviour
(monkeypatch time), check-run reduction to passing/failing/pending, and the
router registration (endpoint present on the private app, absent from
`app/main_public.py`'s app if the BDD suite covers that). Hand-add the
`py_test` target in `home/BUILD` if gazelle does not pick it up
(reference_monolith_gazelle_pytest_targets).

**Step 6: BDD completeness entries** for the new public callables in
`home/tests/bdd_api_test.py` (copy an existing entry's shape).

**Step 7: Format + commit.**
Run: `bazel/tools/format/fast-format.sh`
`git add -A && git commit -m "feat(monolith): add /api/home/dashboard aggregation endpoint"`

---

### Task 2: Cluster chat agent (`POST /api/chat/cluster`)

**Files:**
- Create: `projects/monolith/chat/cluster_agent.py`
- Create: `projects/monolith/chat/cluster_agent_test.py`
- Modify: `projects/monolith/chat/router.py` (new endpoint)
- Modify: `projects/monolith/cluster/api.py` if tool internals are not exported

**Step 1: Read `chat/explorer.py`, `chat/router.py` (/explore endpoint),
`cluster/mcp.py`, `cluster/api.py`, `agent/checks.py`.** The five k8s MCP tools
in `cluster/mcp.py` wrap `KubernetesClient` + `cluster.summarize`; the new agent
tools call the same internals (through the cluster facade), NOT the MCP layer.
Read-only only: do NOT wire `k8s_sync_argocd_app`.

**Step 2: Write `chat/cluster_agent.py`** mirroring `explorer.py`:

```python
SYSTEM_PROMPT = """\
You are the homelab cluster assistant on Joe's private dashboard. You inspect a
Kubernetes homelab (GitOps via ArgoCD, observability via SigNoz) with read-only
tools and answer operational questions plainly.

Workflow: start with health_summary for "what's broken" questions; use
list_resources/get_resource to drill in; use pod_logs and events for root cause;
use firing_alerts for alert state. Quote concrete evidence (restart counts, log
lines, event messages) in your answer. Be concise: a few sentences, not a report.
Never invent resources you did not observe."""

@dataclass
class ClusterDeps:
    emitter: SSEEmitter

def create_cluster_agent() -> Agent[ClusterDeps]:
    # same OpenAIChatModel qwen3.6-27b + LLAMA_CPP_URL wiring as explorer.py
```

Tools (each emits a `tool_call` SSE event `{"tool": name, "args": {...}}` before
running, so the panel can show activity):
- `health_summary()` — same body as `k8s_health_summary`
- `list_resources(kind, namespace=None, label_selector=None)` — capped rows
- `get_resource(kind, name, namespace=None)`
- `pod_logs(namespace, pod, container=None, tail_lines=200, grep=None)`
- `get_events(namespace=None, involved=None)` — mirror the mcp tool signature
  after reading it (lines 143+ of cluster/mcp.py)
- `firing_alerts()` — via agent facade `check_firing_alerts()`

Each tool returns a compact `str`/json.dumps of the summarize output, and is
wrapped in try/except returning `f"error: {exc}"` so one failed kubectl read
does not kill the stream.

**Step 3: Add the endpoint to `chat/router.py`** mirroring `/explore` exactly
(module-level lazy singleton `get_cluster_agent()`, `ExploreRequest` reused or a
twin `ClusterChatRequest`, same `generate()` shape, `text_chunk`/`done`/`error`
events, `StreamingResponse(..., media_type="text/event-stream")`).

**Step 4: Tests** (`import pytest` literal): agent factory constructs (tools
registered by name), tool error wrapping returns a string not an exception, and
the SSE event ordering for a faked run. Follow whatever pattern
`chat/` tests use for the explorer (read them first; if the explorer has no
agent tests, test the tool wrapper functions directly).

**Step 5: Format + commit** `feat(monolith): add read-only cluster chat agent at /api/chat/cluster`.

---

### Task 3: Frontend dashboard page

**Files:**
- Rewrite: `projects/monolith/frontend/src/routes/private/+page.svelte`
- Rewrite: `projects/monolith/frontend/src/routes/private/+page.server.js`
- Create: `projects/monolith/frontend/src/routes/private/chat-cluster/+server.js`
- Create: `projects/monolith/frontend/src/lib/private/launcher.js`
- Create: `projects/monolith/frontend/src/lib/private/components/ClusterChatPanel.svelte`

**Step 1: Read the current page fully** (it is being rewritten, but the
quick-capture textarea, ⌘K knowledge-search overlay, ⌘I ingest mode, the clock,
and the schedule active/past logic are KEPT: lift them as-is). Read
`routes/private/chat/+server.js` for the SSE proxy pattern and
`lib/public/apps.js`.

**Step 2: `+page.server.js`:** keep the existing actions (`capture`, `ingest`,
`search`, `preview`), delete the dead `/api/home` todo fetch and `save` action,
and load:

```js
const [dashRes, dailyRes, weeklyRes] = await Promise.all([
  fetch(`${API_BASE}/api/home/dashboard`, { signal: AbortSignal.timeout(15000) }).catch(() => ({ ok: false })),
  fetch(`${API_BASE}/api/knowledge/tasks/daily`, { signal: AbortSignal.timeout(10000) }).catch(() => ({ ok: false })),
  fetch(`${API_BASE}/api/knowledge/tasks/weekly`, { signal: AbortSignal.timeout(10000) }).catch(() => ({ ok: false })),
]);
return {
  dashboard: dashRes.ok ? await dashRes.json() : null,
  tasksDaily: dailyRes.ok ? (await dailyRes.json()).tasks : [],
  tasksWeekly: weeklyRes.ok ? (await weeklyRes.json()).tasks : [],
};
```

Add a `toggleTask` action that PATCHes `/api/knowledge/tasks/{note_id}` with
`{status}` (read `knowledge/tasks_router.py` + `store.patch_task` first for the
accepted fields and status vocabulary; use what "done"/"open" actually are).

**Step 3: `lib/private/launcher.js`:** the private launcher registry:

```js
import { apps } from "$lib/public/apps.js";
export const launcher = [
  { label: "Notes", desc: "capture + search", href: "/app/notes" },
  { label: "Review", desc: "knowledge review queue", href: "/review" },
  { label: "Chat", desc: "knowledge graph explorer", href: "/chat" },
  { label: "SigNoz", desc: "logs, traces, metrics", href: "/app/signoz" },
  { label: "ArgoCD", desc: "GitOps deploys", href: "/app/argocd" },
  { label: "BuildBuddy", desc: "CI", href: "https://jomcgi.buildbuddy.io", external: true },
  { label: "GitHub", desc: "jomcgi/homelab", href: "https://github.com/jomcgi/homelab", external: true },
  { label: "Docs", desc: "runbooks + ADRs", href: "https://jomcgi.dev/docs", external: true },
  ...apps.map((a) => ({ label: a.label, desc: a.desc, href: a.href })),
];
```

Verify each internal href against the real private routes before committing
(e.g. the chat page path as the browser sees it, /app/signoz and /app/argocd
exist per the path-ingress setup; check `hooks.js` reroute so hrefs are the
browser-visible un-prefixed paths).

**Step 4: Rewrite `+page.svelte`.** Layout: a header row (date + clock, kept),
then a CSS grid of cards, mobile-first single column, 2 to 3 columns >= 900px:

1. **capture** card: the existing textarea + hints (lifted).
2. **today** card: calendar events with the existing past/active treatment, but
   as a fuller timeline (no 10rem max-height crush; show location `ev.location`
   if the event shape has it: check `home/schedule.py` event dict keys and
   render what exists). Below events: tasks. Weekly tasks bold, then daily
   tasks with a checkbox-style toggle calling the `toggleTask` action.
3. **cluster** card: from `data.dashboard.health` + `.alerts`: a single
   healthy/broken headline (green tick / count of unhealthy grouped by kind),
   list of unhealthy workload names, firing alert names. Include the
   `deploy` info if present in stats-like payload (skip if absent).
4. **shipping** card: open PRs (number, title, ci status dot), recent merges.
   Links to github.com PR urls, `target="_blank" rel="noopener"`.
5. **queues** card: review queue count, gaps count, scheduler jobs with
   non-ok last_status surfaced first (name + last_status + relative time).
6. **launcher** card (or a full-width strip): the launcher grid, styled like
   the existing links grid but real hrefs.
7. **ask the cluster** card: `<ClusterChatPanel />`.

Sections whose data is `null`/`{error}` render the card with a one-line
`unavailable` state, never crash SSR (the old page's defensive-defaults comment
is the cautionary tale). Client-side refresh: `$effect` interval re-fetching
`/api/home/dashboard` every 60s via a tiny `GET` handler added to the
`chat-cluster`-style proxy or simply `fetch("?/...")`? No: add
`routes/private/dashboard-data/+server.js` GET proxy returning the backend JSON,
and have the interval update a `$state` copy. Keep the ⌘K search overlay and
keyboard handlers exactly as they are today.

Style: reuse the existing custom properties and typographic scale; cards are
bordered boxes (`border: 0.06rem solid var(--border)`) with the small uppercase
`section-label` headers, consistent with the current aesthetic. No component
library, no new fonts, no gradients.

**Step 5: `ClusterChatPanel.svelte`:** self-contained component (~200 lines):
message list (user/assistant), a one-line input, streaming via
`fetch("/dashboard-chat", {method: "POST", body: JSON.stringify({message, history})})`
reading the SSE body like the explorer page does (copy its reader/parse loop,
minus graph events), rendering `tool_call` events as small dim status lines
(`> pod_logs monolith...`) and `text_chunk` into the current assistant bubble.
History kept in component `$state`, capped at the last 12 turns sent. Wait:
route the POST to `routes/private/dashboard-chat/+server.js` (create it: same
proxy shape as `routes/private/chat/+server.js` but targeting
`${API_BASE}/api/chat/cluster`, 120s timeout).

Reconcile naming: ONE proxy route name, `dashboard-chat`, used by both the
component fetch and the created `+server.js` file (adjust Step 1's `chat-cluster`
path to `dashboard-chat`).

**Step 6: Sanity-render.** `pnpm` type/check is NOT run locally per repo rules;
instead re-read the diff for Svelte 5 runes mistakes (`$state` outside
component top level, missing `$derived` deps) and confirm every `data.` access
has a null-safe path. Do not run `vite build` (it clobbers BUILD on macOS).

**Step 7: Format + commit** `feat(monolith): private dashboard page with launcher and cluster chat`.

---

### Task 4: Drop the nav on the private tier

**Files:**
- Modify: `projects/monolith/frontend/src/routes/+layout.svelte`
- Create: `projects/monolith/frontend/src/routes/private/+layout.svelte`

**Step 1:** In root `+layout.svelte`, add `isPrivate` to the `hideNav`
derivation (the whole private tier drops the shared Nav):

```js
let hideNav = $derived(
  isPrivate ||
    /^\/(public\/|private\/)?app\//.test($page.url.pathname) ||
    ... (existing conditions unchanged)
);
```

Update the comment block: private tier renders its own minimal chrome via
`routes/private/+layout.svelte` (dashboard IS the nav). Note `activeRoute`
"review" logic becomes vestigial for private but `/review` may still be reachable
on other hosts; leave it unless clearly dead after this change (check where
`route="review"` renders: only in Nav, which private no longer shows; public
hosts hide REVIEW already, so removing the review branch is fine IF nothing else
consumes it; verify then decide, prefer leaving it).

**Step 2:** Create `routes/private/+layout.svelte`: renders `{@render children()}`
plus, on every private path EXCEPT the dashboard root, a tiny fixed home link
(top-left, `~ dashboard`, styled like the docs back-link):

```svelte
<script>
  import { page } from "$app/stores";
  let { children } = $props();
  // Browser path is un-prefixed (hooks.js reroute); the dashboard root is "/".
  let onDashboard = $derived($page.url.pathname === "/");
  // /app/* render their own chrome full-screen; don't overlay them.
  let suppress = $derived(/^\/(private\/)?app\//.test($page.url.pathname));
</script>
{#if !onDashboard && !suppress}
  <a class="dash-home" href="/">&larr; dashboard</a>
{/if}
{@render children()}
```

Check how `/private/demos/*` and `/private/review` currently look before
choosing z-index/position so the link does not collide with their own topbars
(suppress on demos too if it collides).

**Step 3: Format + commit** `feat(monolith): private tier drops shared nav for dashboard-first chrome`.

---

### Task 5: Chart bumps + PR

**Steps:**
1. `bazel/tools/git/bump-chart.sh projects/monolith`
2. `bazel/tools/git/bump-chart.sh projects/monolith-public` (shared frontend code
   ships in both tiers; docs manifests regenerated too)
3. `bazel/tools/format/fast-format.sh`, commit
   `chore: bump monolith and monolith-public charts for private dashboard`
4. Push branch, `gh pr create` with a body summarizing the feature (design doc
   link, no em-dashes), then hand back for the end-of-PR review.

---

## Testing strategy

- All pytest suites run on BuildBuddy CI after push; nothing locally.
- Frontend has no unit harness here; correctness leans on the end-of-PR Opus
  review plus post-merge live verification (render the deployed chart, then
  drive the page).
- Post-merge verification (main session, not a subagent): watch ArgoCD sync,
  confirm new chart versions live, then curl/playwright the dashboard and one
  cluster-chat turn.
