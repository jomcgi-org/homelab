# Monolith

The monolith is a single FastAPI + SvelteKit service that hosts most of this
homelab's applications behind one deployment. It combines a personal knowledge
graph, a Discord chat agent, and a handful of small public apps (hikes, trips,
stars, ships, world cup odds, campsites) with a shared database, scheduler, and
public-facing website served at [jomcgi.dev](https://jomcgi.dev).

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

Two tiers run side by side in the same process:

- **Public tier**: read-only, unauthenticated routes served at jomcgi.dev
  (health check, hikes, trips, stars, ships, world cup, campsites, docs, the
  knowledge graph's public views). These use a restricted `public_reader`
  database role and are the only traffic that reaches the public internet
  (via Cloudflare).
- **Private tier**: authenticated apps and APIs (knowledge graph editing,
  chat/Discord agent, goosecracker agent orchestration, task management)
  reachable only from inside the cluster's ingress.

## Key subdirectories

| Path                                                                                       | What it is                                                                                 |
| ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------ |
| `app/`                                                                                     | FastAPI entrypoint, app wiring, lifespan, logging, OpenTelemetry setup                     |
| `frontend/`                                                                                | SvelteKit app: public website, private app UIs, visual regression tests                    |
| `chart/`                                                                                   | Helm chart for the service (templates, migrations, dashboards)                             |
| `deploy/`                                                                                  | ArgoCD Application, Helm values, and GitOps wiring for this cluster                        |
| `knowledge/`                                                                               | The knowledge graph: notes, raw capture ingest, chunking, gap tracking, gardener           |
| `chat/`                                                                                    | Discord bot integration, chat history store, summarizer, goosecracker orchestration client |
| `goosecracker/`                                                                            | Orchestration layer that dispatches agent tasks to the Firecracker-hosted goose agent      |
| `agent/`                                                                                   | MCP tool surface and routine job registry for Claude-driven automation                     |
| `scheduler/`                                                                               | Postgres-backed job scheduler shared by all domains                                        |
| `shared/`                                                                                  | Cross-domain database session/engine setup and test helpers                                |
| `hikes/`, `ships/`, `stars/`, `trips/`, `worldcup/`, `campsites/`, `dr_jobs/`, `grimoire/` | Individual small apps, each with their own routes and models                               |
| `claude_routines/`                                                                         | Version-controlled YAML definitions for scheduled claude.ai agent routines                 |
| `e2e/`                                                                                     | End-to-end tests spanning the frontend and backend together                                |

## Deployment

The monolith is packaged as a Helm chart (`chart/`) and published as an OCI
artifact. Images are built dual-arch (x86_64 and aarch64) with apko in CI, and
the chart version is bumped alongside the image tag. ArgoCD (`deploy/`) tracks
a pinned chart version by OCI reference and syncs it into the cluster; there
is no image-updater in the loop. A push to the repository triggers BuildBuddy
CI to run tests, build and push images, and (on `main`) cut the new chart
version that ArgoCD then rolls out.

Database schema changes go through Atlas migrations checked in under
`chart/migrations/`, applied by an in-cluster Atlas operator rather than at
application startup.
