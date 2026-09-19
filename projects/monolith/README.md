# Monolith

The monolith is the FastAPI + SvelteKit application suite behind most of this
homelab. It combines a personal knowledge graph, a Discord chat agent, the
EmberVM-backed software factory, and small public apps (including Grimoire,
hikes, trips, stars, and ships) over a shared Postgres data plane. Separate
private, public, and agent compositions ship only the routes and code each
audience needs; [jomcgi.dev](https://jomcgi.dev) is the public surface.

## Architecture

The backend is a FastAPI app (`app/main.py`) organized into domains, one
directory per feature area, each owning its own routes, models, and tests
colocated as `*_test.py` files next to the code they cover. Domains talk to a
shared Postgres database (`shared/`) through SQLModel, and a Postgres-backed
scheduler (`scheduler/`) drives background jobs (ingest, retention, digests,
backfills) without a separate workflow engine.

The frontend is a SvelteKit app (`frontend/`) that renders both the public
website and the private app surfaces. Public routes proxy to the backend's
public API through `+page.server.js` loaders so pages render server-side and
stay cacheable at the edge; private routes talk to the authenticated API.

The deployed application has distinct audience surfaces:

- **Public tier**: read-only, unauthenticated routes served at jomcgi.dev
  (health, public apps, docs, and published factory and knowledge views). It is
  a pruned binary on the restricted `public_reader` role, with narrowly scoped
  writers for the two public chat domains.
- **Private tier**: authenticated apps, APIs, Discord integration, knowledge
  editing, and factory controls behind the private ingress.
- **Agent tier**: a separate pruned MCP server for EmberVM guests. It exposes
  the bounded knowledge and Kubernetes observation tools described in
  [ARCHITECTURE.md](ARCHITECTURE.md#7-mcp-surface), not the private catalogue.
- **Friends surface**: only the moving planner and its browser API, protected by
  its own authentik policy.

The tier boundaries are enforced through separate compositions, database
roles, and ingress policy; see [ARCHITECTURE.md](ARCHITECTURE.md) sections 1 to
3.
The hazard model for this boundary is [STPA.md](STPA.md).

The shipped **Factory** selects bounded issue work, asks a per-task **Planner**
to build a DAG, and has an **Executor** run its role-specific nodes. Legacy
`factory_conductor` names refer to that task planner, not to the proposed
operator-facing Conductor. The top-level Conductor specification and expanded
MCP review and steering interface remain follow-up work in
[#5785](https://github.com/jomcgi-org/homelab/issues/5785) and
[#5788](https://github.com/jomcgi-org/homelab/issues/5788); this README does not
present them as shipped.

## Trust and safety

Discord engagement runs behind a per-server, per-user trust ledger. Narrow
regex heuristics, an asynchronous LLM intent classifier, and a shadow-first
random forest feed one score. The thresholds are environment-overridable; a
pardon restores the score and relabels recent events, so a wrong lockout
becomes corrective training data. Current state:
[ARCHITECTURE.md](ARCHITECTURE.md#5-chat).

## Key subdirectories

| Path                                                                                       | What it is                                                                                 |
| ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------ |
| `app/`                                                                                     | FastAPI entrypoint, app wiring, lifespan, logging, OpenTelemetry setup                     |
| `frontend/`                                                                                | SvelteKit app: public website, private app UIs                    |
| `chart/`                                                                                   | Helm chart for the service (templates, migrations, dashboards)                             |
| `deploy/`                                                                                  | ArgoCD Application, Helm values, and GitOps wiring for this cluster                        |
| `knowledge/`                                                                               | Evidence ingestion, extraction, retrieval, public views, and knowledge interventions       |
| `chat/`                                                                                    | Discord bot, trust and safety, triggers, reminders, summaries, and session adapter          |
| `factory/`                                                                                 | Factory execution, task planning, DAG orchestration, operator controls, and public snapshots |
| `scheduler/`                                                                               | Postgres-backed job scheduler shared by all domains                                        |
| `shared/`                                                                                  | Cross-domain database session/engine setup and test helpers                                |
| `grimoire/`, `hikes/`, `ships/`, `stars/`, `trips/`, `worldcup/`, `campsites/`, `dr_jobs/` | Individual public data products, each with its own routes and models                       |
| `e2e/`                                                                                     | End-to-end tests spanning the frontend and backend together                                |

## Deployment

The monolith is packaged as a Helm chart (`chart/`) and published as an OCI
artifact. Images are built amd64-only with apko in CI. A
merge to `main` publishes the chart and writes its new version back to the
repository. Production runs on the GKE hub, where Kargo promotes new chart
versions to the `monolith` and `monolith-public` ArgoCD Applications. The home
deployment is dormant, and its pinned revision remains a revert record rather
than the production source of truth. See
[Platform architecture: GitOps and delivery](../platform/ARCHITECTURE.md#4-gitops-and-delivery)
for the current pipeline.

Database schema changes go through Atlas migrations checked in under
`chart/migrations/`, applied by an in-cluster Atlas operator rather than at
application startup.
