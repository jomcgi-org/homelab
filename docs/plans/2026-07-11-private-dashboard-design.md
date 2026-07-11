# Private Dashboard for private.jomcgi.dev

Date: 2026-07-11
Status: Approved (Option A, chosen via brainstorming session)

## Goal

Turn the private.jomcgi.dev landing page into a personal dashboard: a guide to Joe's
life and what's happening, with a strong focus on the homelab and personal projects.
Drop the shared nav bar on the private tier in favour of the dashboard itself being
the navigation. Include an embedded privileged chat panel that can inspect the
cluster using the existing k8s debug tools.

## Decisions made

- Type: live status + launcher (not launcher-only, not a deep ops board).
- Widgets v1: cluster health, today panel (todo + calendar in a more usable format),
  work in flight (PRs/CI), queues and jobs.
- Navigation: the dashboard IS the nav on the private tier. No persistent nav bar on
  private pages; sub-pages get a small "back to dashboard" home link. Public tier
  keeps Nav.svelte untouched.
- Cluster chat: embedded chat widget on the dashboard itself (not a deep link to
  /private/chat, not deferred).

## Architecture (Option A)

One dashboard page, one aggregation endpoint, one new chat agent.

### Frontend

- `frontend/src/routes/private/+page.svelte` becomes the dashboard. Sections:
  - Launcher grid: apps from the existing `$lib/public/apps.js` registry plus
    private destinations (Notes, Review, Chat, SigNoz, ArgoCD at /app/*). A small
    private launcher registry lists the private-only entries.
  - Cluster health card: ArgoCD app sync/health rollup, node/pod health, firing
    alerts.
  - Today card: editable todo (existing `/api/home` PUT flow) plus today's calendar
    events rendered as a timeline (time, title, location), replacing the current
    raw list. More usable format per Joe's request.
  - Work in flight card: open PRs with CI status, recent merges on jomcgi/homelab.
  - Queues and jobs card: knowledge review queue count, gap queue count, scheduler
    job health (failing/stuck jobs surfaced first), agent thread activity.
  - Cluster chat panel: embedded chat UI streaming from the new cluster agent.
- Private layout: `routes/private/+layout.svelte` renders NO nav bar. Sub-pages
  (notes, review, chat, demos) get a minimal home link in that layout. The root
  `+layout.svelte` continues to render Nav.svelte for public routes only.
- Page load: one server-side fetch of `/api/home/dashboard` in `+page.server.js`,
  then client-side interval refresh (60s) of the same endpoint. Per-section errors
  render as degraded cards, not a failed page.

### Backend

- New `GET /api/home/dashboard` in the home domain: fans out server-side to
  existing internals (observability stats, scheduler jobs, knowledge queue counts,
  calendar/today, todo) plus two new collectors:
  - ArgoCD rollup: list Application CRs via the existing cluster read client,
    reduce to {synced, outOfSync, degraded, names of unhealthy apps}.
  - GitHub: open PRs + check status + last few merges via the GitHub API using the
    existing repo token. Cached in-process (~60s TTL) to respect rate limits.
  Each section is independently try/excepted and returns `{error: ...}` on failure
  so one dead collector cannot blank the dashboard.
- New `POST /api/chat/cluster` SSE endpoint: PydanticAI agent (same Qwen model and
  SSEEmitter pattern as chat/explorer.py) whose tools wrap the existing functions
  in cluster/mcp.py (k8s_health_summary, k8s_list_resources, k8s_get_resource,
  k8s_get_pod_logs, k8s_get_events) plus the firing-alerts check. Read-only tools
  only. Stateless, history in the POST body, same as the explorer.

### RBAC

The monolith ClusterRole must cover every verb the new collectors call. The ArgoCD
rollup needs list/get on argoproj.io applications (get already granted, verify
list/watch). k8s tools already run under the existing grants via MCP; the dashboard
introduces no new resource kinds beyond what those tools read.

### Deploy

Shared frontend code changes ship in the monolith image, and the public tier serves
the same frontend build from monolith-public, so BOTH charts get bumped in the PR
(bazel/tools/git/bump-chart.sh for projects/monolith and projects/monolith-public).

## Out of scope (v1)

- Chat session persistence (explorer chat is stateless today; keep parity).
- Refactoring the /private/chat explorer page into reusable components.
- Write-path cluster tools in the chat agent (read-only only).
- Routing the cluster chat to a stronger model (Qwen v1, revisit after use).

## Testing

- Backend: unit tests for the dashboard aggregator (per-section error isolation,
  shape of the rollups) with fake collectors; router registration covered by the
  existing bdd_completeness surface (new public callables need BDD entries).
- Frontend: visual regression mock-data route if cheap; otherwise rely on CI
  build + type checks and post-merge live verification.
- All tests run on BuildBuddy CI on the pushed branch, none locally.
