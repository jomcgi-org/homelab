# Homelab

Personal monorepo. Dev tooling and deployment for my projects.

## Systems

- [**Knowledge pipeline**](projects/monolith/knowledge/) — On-cluster LLM decomposes markdown into structured facts, embeds them, stores in pgvector. Searchable via MCP tools and a SvelteKit frontend.
- [**Agent platform**](projects/agent_platform/) — AI agents in sandboxed Kubernetes pods with RBAC-scoped tool access over NATS JetStream. [Architecture](docs/agents.md).
- [**Discord bot**](projects/monolith/chat/) — LLM-powered chat with vision, web search, and knowledge graph context.
- [**OCI Model Cache**](projects/operators/oci-model-cache/) — Kubernetes operator that syncs ML models from HuggingFace to OCI registries. Compiler-enforced state machines.
- [**Sextant**](projects/sextant/) — Code generator that turns YAML state-machine specs into type-safe Go for operators: invalid transitions are compile errors, idempotency keys are forced into transition signatures. Generates the OCI Model Cache machine, drift-checked in CI.
- [**Build system**](bazel/) — Custom Bazel rules for Helm, Semgrep SAST, and Cloudflare Pages. All builds run remotely via BuildBuddy RBE.

## Applications

- [**Marine tracking**](projects/ships/) — Real-time AIS vessel tracking with a MapLibre GL frontend.
- [**Trip tracker**](projects/trips/) — Reconstruct travel routes from photo EXIF data with elevation profiles.
- [**Stargazer**](projects/stargazer/) — Best stargazing spots in Scotland for the next 72 hours.
- [**Hiking routes**](projects/hikes/) — Scottish route finder with weather-based recommendations.

## Infrastructure patterns

See [docs/security.md](docs/security.md) for the defense-in-depth model and [docs/observability.md](docs/observability.md) for automatic instrumentation.

| Area          | Approach                                                                      |
| ------------- | ----------------------------------------------------------------------------- |
| Ingress       | Cloudflare Tunnel only — nothing exposed directly                             |
| Service mesh  | Linkerd — automatic mTLS and distributed tracing, no code changes             |
| Observability | SigNoz — unified metrics, logs, traces. Kyverno auto-injects OTEL env vars    |
| Policy        | Kyverno — enforces non-root (uid 65532), read-only filesystems                |
| Secrets       | 1Password Operator — OnePasswordItem CRDs, nothing in Git                     |
| Storage       | Longhorn for persistent volumes, SeaweedFS for S3-compatible object storage   |
| Messaging     | NATS JetStream — pub/sub backbone for AIS data, trip points, agent jobs       |
| GPU           | NVIDIA GPU Operator — Qwen 3 + voyage-4-nano on-cluster via llama.cpp         |
| Images        | apko + rules_apko — no Dockerfiles, dual-arch (x86_64 + aarch64), non-root    |
| CI            | BuildBuddy Workflows — remote build execution, `bazel test //...`, image push |
| GitOps        | ArgoCD — colocated `deploy/` dirs, `kubectl` is read-only                     |

## Repo layout

```
projects/             # All services, operators, websites — colocated with deploy configs
├── platform/         #   Cluster-critical infrastructure (ArgoCD, Linkerd, SigNoz, etc.)
├── agent_platform/   #   Agent services (Context Forge, MCP servers, orchestrator, etc.)
├── monolith/         #   Knowledge graph, Discord bot, task management, frontend
├── ships/            #   Marine vessel tracking
├── trips/            #   Trip tracker
├── stargazer/        #   Dark sky finder
├── hikes/            #   Scottish hiking routes
├── operators/        #   Custom Kubernetes operators
├── websites/         #   Static sites (VitePress, Astro)
└── home-cluster/     #   Auto-generated ArgoCD root kustomization
bazel/                # Build infrastructure (rules, tools, images, semgrep)
docs/                 # Design docs, ADRs, and plans
```

See [docs/contributing.md](docs/contributing.md) for the full structure. Architecture decisions are tracked in [docs/decisions/](docs/decisions/).

## What's next

- **Semgrep rule generation** — RL-finetuned local model for generating Semgrep rules from CVE descriptions. The build system already runs hermetic Semgrep; this closes the loop by generating the rules themselves.
- **Knowledge graph expansion** — Ingest D&D sourcebooks (via Marker parsing) and repo documentation into the knowledge pipeline, making ADRs and design docs semantically searchable alongside notes.

Full backlog and architecture decisions: [docs/decisions/](docs/decisions/)

---

Built by [Joe McGinley](https://github.com/jomcgi). [MPL-2.0](LICENSE).
