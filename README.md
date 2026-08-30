# Homelab

Personal monorepo. Dev tooling and deployment for my projects.

## Systems

- [**EmberVM**](projects/embervm/): Firecracker microVM orchestrator. An Elixir control plane and a Go node daemon run task, session, serving, stateful, and composite workloads. See its [current architecture](projects/embervm/ARCHITECTURE.md).
- [**Firecracker components**](projects/firecracker/): guest images and shared host/guest utilities retained by EmberVM after the original fc-invoke daemon was retired.
- [**Knowledge pipeline**](projects/monolith/knowledge/): an on-cluster LLM decomposes markdown into structured facts, embeds them, and stores them in pgvector. Searchable via MCP tools and a SvelteKit frontend.
- [**Agent platform**](projects/embervm/ARCHITECTURE.md): AI agents in sandboxed microVM sessions, with the monolith providing the user-facing control plane.
- [**Discord bot**](projects/monolith/chat/): LLM-powered chat with vision, web search, knowledge graph context, and a per-user trust ledger ([ADR chat/003](docs/decisions/chat/003-trust-safety-safeguards.md)).
- [**OCI Model Cache**](projects/operators/oci-model-cache/): Kubernetes operator that syncs ML models from HuggingFace to OCI registries. Compiler-enforced state machines.
- [**Sextant**](projects/sextant/): code generator that turns YAML state-machine specs into type-safe Go for operators. Invalid transitions are compile errors, idempotency keys are forced into transition signatures. Generates the OCI Model Cache machine, drift-checked in CI.
- [**Design system**](projects/design-system/): the shared `--ds-*` token contract the frontends build against. One namespaced vocabulary, three deliberately distinct themes (neobrutalist, ember, Grimoire) that override it inside their own scope class. Rationale in [ADR platform/013](docs/decisions/platform/013-design-system-contract-distinct-themes.md).
- [**Build system**](bazel/): custom Bazel rules for Helm, Semgrep SAST, and Cloudflare Pages. All builds run remotely via BuildBuddy RBE.
- [**Buck2 rules**](buck2/): reusable Buck2 rules for container images (apko/OCI) and Helm charts, the Buck2 counterparts to the Bazel rules, consumable by other Buck2 projects as an external cell.

## Applications

Public apps served by the monolith at [jomcgi.dev/app](https://jomcgi.dev/app):

- [**Marine tracking**](projects/monolith/frontend/src/routes/public/app/ships/): Real-time AIS vessel tracking with a MapLibre GL frontend.
- [**Trip tracker**](projects/monolith/frontend/src/routes/public/app/trips/): Reconstruct travel routes from photo EXIF data with elevation profiles.
- [**Stargazing**](projects/monolith/frontend/src/routes/public/app/stars/): Best stargazing spots in Scotland with cloud-cover and darkness forecasts.
- [**Campsites**](projects/monolith/frontend/src/routes/public/app/campsites/): BC Parks availability crossed with weather.
- [**World Cup 2026 odds**](projects/monolith/frontend/src/routes/public/app/wc2026/): Scotland's qualification odds via an Elo Monte Carlo model.
- [**Hiking routes**](projects/monolith/frontend/src/routes/public/app/hikes/): Scottish route finder with weather-based recommendations.
- [**Grimoire**](projects/monolith/frontend/src/routes/public/app/grimoire/): Postgres-first D&D campaign manager with a public rules and lore explorer.
- [**Notes**](projects/monolith/frontend/src/routes/public/app/notes/): Anonymous chat over an on-cluster Qwen model on a shared public scratchpad.
- [**LLM Leaderboard**](projects/monolith/frontend/src/routes/public/app/llm-leaderboard/): Agentic coding benchmark results across models, from the model-bench harness.
- [**NHS jobs**](projects/monolith/frontend/src/routes/public/app/dr-jobs/): NHS Scotland anaesthetics consultant vacancy aggregator.

## Infrastructure patterns

See [docs/security.md](docs/security.md) for the defense-in-depth model, [projects/monolith/STPA.md](projects/monolith/STPA.md) for the hazard analysis, and [docs/observability.md](docs/observability.md) for automatic instrumentation.

| Area           | Approach                                                                                   |
| -------------- | ------------------------------------------------------------------------------------------ |
| Ingress        | Cloudflare Tunnel only; nothing exposed directly                                           |
| Untrusted code | Firecracker microVMs (EmberVM), STPA hazard model colocated with the monolith              |
| Networking     | Cilium eBPF CNI: WireGuard pod-to-pod encryption, network policy, Hubble metrics, no sidecars |
| Observability  | SigNoz: unified metrics, logs, traces. Kyverno auto-injects OTEL env vars                  |
| Policy         | Kyverno enforces non-root (uid 65532), read-only filesystems                               |
| Secrets        | 1Password Operator, OnePasswordItem CRDs, nothing in Git                                   |
| Storage        | Longhorn for persistent volumes, SeaweedFS for S3-compatible object storage                |
| Messaging      | NATS JetStream: pub/sub backbone for AIS data, trip points, agent jobs                     |
| GPU            | NVIDIA GPU Operator: Qwen3.8-27B via llama.cpp; voyage-4-nano embeddings via llama.cpp     |
| Images         | apko + rules_apko: no Dockerfiles, dual-arch (x86_64 + aarch64), non-root                  |
| CI             | BuildBuddy Workflows: remote build execution, `bazel test //...`, image push               |
| GitOps         | ArgoCD with colocated `deploy/` dirs; `kubectl` is read-only                               |

## Repo layout

```
projects/             # All services, operators, websites, colocated with deploy configs (major dirs shown)
├── platform/         #   Cluster-critical infrastructure (ArgoCD, Cilium, SigNoz, etc.)
├── monolith/         #   Knowledge graph, Discord bot, task management, public apps, frontend
├── monolith-public/  #   Read-only public replica of the monolith
├── mcp/              #   Context Forge gateway + MCP servers
├── inference/        #   On-cluster llama.cpp (Qwen3.8-27B) + llama.cpp embeddings
├── operators/        #   Custom Kubernetes operators
├── sextant/          #   State-machine code generator for operators
├── embervm/          #   Firecracker microVM orchestrator (Elixir control plane + Go node daemon)
├── firecracker/      #   fc-invoke microVM substrate (frozen; embervm forked its node daemon from it)
├── gke-apps/         #   GKE destinations for the app workloads (dormant until cutover)
├── gke-cluster/      #   Auto-generated ArgoCD root kustomization for GKE hub
├── home-cluster/     #   Auto-generated ArgoCD root kustomization
└── platform-gke/     #   GKE cluster-critical infrastructure overlays (Tailscale, ArgoCD, Otel, Cloudflare)
bazel/                # Build infrastructure (rules, tools, images, semgrep)
buck2/                # Reusable Buck2 image/helm/apko rules (consumable as a cell)
docs/                 # Design docs, ADRs, and plans
```

See [docs/contributing.md](docs/contributing.md) for the full structure. Architecture decisions are tracked in [docs/decisions/](docs/decisions/).

## What's next

Active initiatives and their rationale are tracked as ADRs: [docs/decisions/](docs/decisions/)

---

Built by [Joe McGinley](https://github.com/jomcgi). [MPL-2.0](LICENSE).
