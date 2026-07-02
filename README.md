# Homelab

Personal monorepo. Dev tooling and deployment for my projects.

## Systems

- [**Knowledge pipeline**](projects/monolith/knowledge/) — On-cluster LLM decomposes markdown into structured facts, embeds them, stores in pgvector. Searchable via MCP tools and a SvelteKit frontend.
- [**Agent platform**](docs/agents.md): AI agents in sandboxed microVMs with RBAC-scoped tool access, orchestrated from the monolith. See the [orchestrator ADR](docs/decisions/agents/007-agent-orchestrator.md).
- [**Discord bot**](projects/monolith/chat/) — LLM-powered chat with vision, web search, and knowledge graph context.
- [**OCI Model Cache**](projects/operators/oci-model-cache/) — Kubernetes operator that syncs ML models from HuggingFace to OCI registries. Compiler-enforced state machines.
- [**Sextant**](projects/sextant/) — Code generator that turns YAML state-machine specs into type-safe Go for operators: invalid transitions are compile errors, idempotency keys are forced into transition signatures. Generates the OCI Model Cache machine, drift-checked in CI.
- [**Build system**](bazel/) — Custom Bazel rules for Helm, Semgrep SAST, and Cloudflare Pages. All builds run remotely via BuildBuddy RBE.
- [**Buck2 rules**](buck2/) — Reusable Buck2 rules for container images (apko/OCI) and Helm charts: the Buck2 counterparts to the Bazel rules, consumable by other Buck2 projects as an external cell.

## Applications

Public apps served by the monolith at [jomcgi.dev/app](https://jomcgi.dev/app):

- [**Marine tracking**](projects/monolith/frontend/src/routes/public/app/ships/): Real-time AIS vessel tracking with a MapLibre GL frontend.
- [**Trip tracker**](projects/monolith/frontend/src/routes/public/app/trips/): Reconstruct travel routes from photo EXIF data with elevation profiles.
- [**Stargazing**](projects/monolith/frontend/src/routes/public/app/stars/): Best stargazing spots in Scotland with cloud-cover and darkness forecasts.
- [**Campsites**](projects/monolith/frontend/src/routes/public/app/campsites/): BC Parks availability crossed with weather.
- [**World Cup 2026 odds**](projects/monolith/frontend/src/routes/public/app/wc2026/): Scotland's qualification odds via an Elo Monte Carlo model.
- [**Hiking routes**](projects/hikes/): Scottish route finder with weather-based recommendations.

## Infrastructure patterns

See [docs/security.md](docs/security.md) for the defense-in-depth model and [docs/observability.md](docs/observability.md) for automatic instrumentation.

| Area          | Approach                                                                                  |
| ------------- | ----------------------------------------------------------------------------------------- |
| Ingress       | Cloudflare Tunnel only — nothing exposed directly                                         |
| Service mesh  | Linkerd — automatic mTLS and distributed tracing, no code changes                         |
| Observability | SigNoz — unified metrics, logs, traces. Kyverno auto-injects OTEL env vars                |
| Policy        | Kyverno — enforces non-root (uid 65532), read-only filesystems                            |
| Secrets       | 1Password Operator — OnePasswordItem CRDs, nothing in Git                                 |
| Storage       | Longhorn for persistent volumes, SeaweedFS for S3-compatible object storage               |
| Messaging     | NATS JetStream — pub/sub backbone for AIS data, trip points, agent jobs                   |
| GPU           | NVIDIA GPU Operator: Qwen3.6-35B-A3B MoE via vLLM; voyage-4-nano embeddings via llama.cpp |
| Images        | apko + rules_apko — no Dockerfiles, dual-arch (x86_64 + aarch64), non-root                |
| CI            | BuildBuddy Workflows — remote build execution, `bazel test //...`, image push             |
| GitOps        | ArgoCD — colocated `deploy/` dirs, `kubectl` is read-only                                 |

## Repo layout

```
projects/             # All services, operators, websites, colocated with deploy configs (major dirs shown)
├── platform/         #   Cluster-critical infrastructure (ArgoCD, Linkerd, SigNoz, etc.)
├── monolith/         #   Knowledge graph, Discord bot, task management, public apps, frontend
├── monolith-public/  #   Read-only public replica of the monolith
├── mcp/              #   Context Forge gateway + MCP servers
├── inference/        #   On-cluster vLLM (Qwen3.6) + llama.cpp embeddings
├── hikes/            #   Scottish hiking routes (standalone Pages frontend)
├── operators/        #   Custom Kubernetes operators
├── websites/         #   Static sites (VitePress, Astro)
└── home-cluster/     #   Auto-generated ArgoCD root kustomization
bazel/                # Build infrastructure (rules, tools, images, semgrep)
buck2/                # Reusable Buck2 image/helm/apko rules (consumable as a cell)
docs/                 # Design docs, ADRs, and plans
```

See [docs/contributing.md](docs/contributing.md) for the full structure. Architecture decisions are tracked in [docs/decisions/](docs/decisions/).

## What's next

Active initiatives and their rationale are tracked as ADRs: [docs/decisions/](docs/decisions/)

---

Built by [Joe McGinley](https://github.com/jomcgi). [MPL-2.0](LICENSE).
